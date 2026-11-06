from __future__ import annotations
import math, threading, time, logging, os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
IST = ZoneInfo("Asia/Kolkata")
BASE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}
session = requests.Session()
lock = threading.Lock()
cache = {"at": 0.0, "data": None}
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("niftylab.market")
app = FastAPI(title="NIFTY Option Chain Intelligence", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

def now():
    return datetime.now(IST).isoformat(timespec="seconds")

def clean_leg(value):
    keys = ("lastPrice", "change", "pChange", "totalTradedVolume", "openInterest",
            "changeinOpenInterest", "impliedVolatility", "bidprice", "askPrice",
            "bidQty", "askQty")
    return {key: value.get(key) for key in keys}

def get_market(force=False, expiry=None):
    """Fetch genuine NIFTY option-chain data using a free, read-only Upstox Analytics Token.

    No order/trading endpoints are used. If the token is absent or the provider fails,
    return UNAVAILABLE/STALE rather than synthesising prices or bypassing NSE controls.
    """
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        return {
            "status": "UNAVAILABLE", "source": "Upstox read-only market-data API",
            "source_timestamp": None, "received_at": now(), "spot": None,
            "expiry_dates": [], "rows": [],
            "error": "UPSTOX_ACCESS_TOKEN is not configured. Add your free read-only Analytics Token in Render environment variables.",
            "underlying": "NIFTY",
        }

    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    with lock:
        try:
            # Cache the contract master briefly; it contains the available expiry dates.
            if not cache.get("contracts") or time.time() - cache.get("contracts_at", 0) > 600:
                contract_response = session.get(
                    "https://api.upstox.com/v2/option/contract",
                    params={"instrument_key": "NSE_INDEX|Nifty 50"},
                    headers=headers, timeout=12,
                )
                contract_response.raise_for_status()
                contract_payload = contract_response.json()
                contracts = contract_payload.get("data") or []
                expiry_dates = sorted({str(item.get("expiry")) for item in contracts if item.get("expiry")})
                if not expiry_dates:
                    raise RuntimeError("Upstox returned no NIFTY option expiries")
                cache["contracts"] = expiry_dates
                cache["contracts_at"] = time.time()
            else:
                expiry_dates = cache["contracts"]

            chosen_expiry = expiry if expiry in expiry_dates else expiry_dates[0]
            cached = cache.get("data")
            if (not force and cached and cached.get("expiry") == chosen_expiry
                    and time.time() - cache.get("at", 0) < 5):
                return cached

            response = session.get(
                "https://api.upstox.com/v2/option/chain",
                params={"instrument_key": "NSE_INDEX|Nifty 50", "expiry_date": chosen_expiry},
                headers=headers, timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise RuntimeError("Upstox option-chain response was not successful")
            chain = payload.get("data") or []
            rows = []

            def map_leg(option):
                option = option or {}
                md = option.get("market_data") or {}
                greeks = option.get("option_greeks") or {}
                ltp = md.get("ltp")
                close = md.get("close_price")
                try:
                    change = float(ltp) - float(close) if ltp is not None and close not in (None, 0) else None
                    pchange = (change / float(close) * 100) if change is not None else None
                except (TypeError, ValueError, ZeroDivisionError):
                    change, pchange = None, None
                oi, prev_oi = md.get("oi"), md.get("prev_oi")
                try:
                    change_oi = float(oi) - float(prev_oi) if oi is not None and prev_oi is not None else None
                except (TypeError, ValueError):
                    change_oi = None
                return {
                    "lastPrice": ltp, "change": change, "pChange": pchange,
                    "totalTradedVolume": md.get("volume"), "openInterest": oi,
                    "changeinOpenInterest": change_oi, "impliedVolatility": greeks.get("iv"),
                    "bidprice": md.get("bid_price"), "askPrice": md.get("ask_price"),
                    "bidQty": md.get("bid_qty"), "askQty": md.get("ask_qty"),
                    "delta": greeks.get("delta"), "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"), "vega": greeks.get("vega"),
                }

            spot = None
            for item in chain:
                strike = item.get("strike_price")
                if strike is None:
                    continue
                if spot is None:
                    spot = item.get("underlying_spot_price")
                rows.append({
                    "strike": strike, "expiry": item.get("expiry") or chosen_expiry,
                    "ce": map_leg(item.get("call_options")),
                    "pe": map_leg(item.get("put_options")),
                })
            if not rows or spot is None:
                raise RuntimeError("Upstox returned an empty or incomplete NIFTY option chain")

            data = {
                "status": "LIVE", "source": "Upstox Analytics Token · read-only API",
                "source_timestamp": None, "received_at": now(), "spot": spot,
                "expiry_dates": expiry_dates, "expiry": chosen_expiry,
                "rows": sorted(rows, key=lambda item: float(item["strike"])),
                "underlying": "NIFTY",
                "note": "Provider response received time is shown; exchange tick timestamp is not supplied by this endpoint.",
            }
            cache.update(at=time.time(), data=data)
            return data
        except Exception as exc:
            logger.exception("Upstox option-chain fetch failed: %s", exc)
            if cache.get("data") and (expiry is None or cache["data"].get("expiry") == expiry):
                stale = dict(cache["data"])
                stale.update(status="STALE", age_seconds=round(time.time() - cache["at"]), error=str(exc))
                return stale
            return {
                "status": "UNAVAILABLE", "source": "Upstox Analytics Token · read-only API",
                "source_timestamp": None, "received_at": now(), "spot": None,
                "expiry_dates": cache.get("contracts", []), "rows": [],
                "error": str(exc), "underlying": "NIFTY",
            }

def number(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0

def expiry_analysis(rows, spot):
    calls = [(r["strike"], number(r["ce"].get("openInterest"))) for r in rows]
    puts = [(r["strike"], number(r["pe"].get("openInterest"))) for r in rows]
    candidates = sorted({strike for strike, _ in calls + puts})
    payouts = []
    for settlement in candidates:
        payout = sum(oi * max(0, settlement - strike) for strike, oi in calls)
        payout += sum(oi * max(0, strike - settlement) for strike, oi in puts)
        payouts.append((payout, settlement))
    max_pain = min(payouts)[1] if payouts else None
    call_oi = sum(oi for _, oi in calls)
    put_oi = sum(oi for _, oi in puts)
    call_change = sum(number(r["ce"].get("changeinOpenInterest")) for r in rows)
    put_change = sum(number(r["pe"].get("changeinOpenInterest")) for r in rows)
    ce_sorted = sorted(rows, key=lambda r: number(r["ce"].get("openInterest")), reverse=True)
    pe_sorted = sorted(rows, key=lambda r: number(r["pe"].get("openInterest")), reverse=True)
    atm = round(float(spot) / 50) * 50
    return {
        "atm": atm, "max_pain": max_pain,
        "pcr": round(put_oi / call_oi, 3) if call_oi else None,
        "total_ce_oi": call_oi, "total_pe_oi": put_oi,
        "total_ce_change_oi": call_change, "total_pe_change_oi": put_change,
        "support": [{"strike": r["strike"], "oi": number(r["pe"].get("openInterest")),
                     "change_oi": number(r["pe"].get("changeinOpenInterest"))} for r in pe_sorted[:3]],
        "resistance": [{"strike": r["strike"], "oi": number(r["ce"].get("openInterest")),
                        "change_oi": number(r["ce"].get("changeinOpenInterest"))} for r in ce_sorted[:3]],
    }

def rank_watchlist(rows, atm):
    result = []
    for row in rows:
        for side in ("CE", "PE"):
            leg = row[side.lower()]
            ltp, volume, oi = (number(leg.get(k)) for k in ("lastPrice", "totalTradedVolume", "openInterest"))
            bid, ask = number(leg.get("bidprice")), number(leg.get("askPrice"))
            if min(ltp, volume, oi, bid, ask) <= 0 or ask < bid:
                continue
            spread = (ask - bid) / ltp * 100
            if spread > 2:
                continue
            premium_change = number(leg.get("pChange"))
            oi_change = number(leg.get("changeinOpenInterest"))
            score = 50 + max(-15, min(15, premium_change)) + (7 if oi_change > 0 else -4)
            score += 8 if abs(float(row["strike"]) - atm) <= 50 else 0
            score += 5 if spread < 0.75 else 0
            result.append({
                "direction": "BUY " + side, "strike": row["strike"], "expiry": row["expiry"],
                "ltp": ltp, "bid": bid, "ask": ask, "spread_pct": round(spread, 2),
                "volume": volume, "oi": oi, "change_oi": oi_change,
                "iv": number(leg.get("impliedVolatility")), "premium_change_pct": premium_change,
                "score": round(max(0, min(100, score)), 1), "status": "WATCH",
                "generated_at": now(),
                "reason": "Rules-based option-chain screen only. Spot and premium candle confirmation is not connected; this is not an entry alert.",
            })
    return sorted(result, key=lambda item: item["score"], reverse=True)[:12]

@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "time": now(), "data_status": cache["data"].get("status", "NOT_FETCHED") if cache["data"] else "NOT_FETCHED"}

@app.get("/api/market")
def market(force: bool = False):
    return get_market(force=force)

@app.get("/api/analysis")
def analysis(expiry: str | None = Query(default=None)):
    market_data = get_market(expiry=expiry)
    if market_data["status"] != "LIVE" or market_data.get("spot") is None:
        return {"status": "WAIT", "message": "Verified live NSE data is unavailable. Analysis is paused.",
                "market": market_data, "signals": [], "metrics": None, "rows": []}
    available = market_data["expiry_dates"]
    chosen = expiry if expiry in available else (available[0] if available else None)
    rows = [row for row in market_data["rows"] if row["expiry"] == chosen]
    if not rows:
        return {"status": "WAIT", "message": "No contracts returned for the selected expiry.",
                "market": market_data, "signals": [], "metrics": None, "rows": []}
    metrics = expiry_analysis(rows, market_data["spot"])
    near = sorted([r for r in rows if abs(float(r["strike"]) - metrics["atm"]) <= 500],
                  key=lambda r: r["strike"])
    return {
        "status": "LIVE", "market": market_data, "expiry": chosen,
        "metrics": metrics, "scan_count": len(near), "rows": rows, "near_rows": near,
        "signals": rank_watchlist(near, metrics["atm"]),
        "disclaimer": "Screening heuristic, not a trained or validated prediction model. No candle-confirmed entry is generated.",
    }

@app.get("/api/alerts")
def alerts():
    market_data = get_market()
    if market_data["status"] != "LIVE":
        return {"status": "PAUSED", "items": [], "message": "Alerts paused because live market data is not verified."}
    return {"status": "MONITORING", "items": [],
            "message": "No confirmed entry alerts. Spot and option-premium candle confirmation is not connected yet."}
