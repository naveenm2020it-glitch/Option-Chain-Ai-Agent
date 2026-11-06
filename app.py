from __future__ import annotations
import math, threading, time, logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from indian_option_mcp_test import test_provider as test_indian_option_mcp_provider

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

def get_market(force=False):
    # Never fetch NSE implicitly from dashboard polling. Only the explicit
    # Test connection / Fetch snapshot action may make one upstream request.
    if force:
        result = test_indian_option_mcp_provider()
        if result.get("status") == "CONNECTED" and result.get("rows"):
            market_data = {
                "status": "LIVE",
                "source": "NSE website endpoint via Indian-Option-MCP-compatible adapter",
                "source_timestamp": result.get("exchange_timestamp"),
                "received_at": result.get("received_at") or now(),
                "spot": result.get("spot"),
                "expiry_dates": result.get("expiry_dates") or [],
                "rows": result.get("rows") or [],
                "error": None,
                "underlying": "NIFTY",
                "http_status": result.get("http_status"),
                "elapsed_ms": result.get("elapsed_ms"),
                "disclaimer": "Unofficial NSE website endpoint; availability and permission are not established. One user-triggered snapshot only."
            }
            cache["data"] = market_data
            cache["at"] = time.time()
        else:
            return {
                "status": "UNAVAILABLE", "source": "NSE option-chain endpoint",
                "source_timestamp": result.get("exchange_timestamp"),
                "received_at": result.get("received_at") or now(),
                "spot": result.get("spot"), "expiry_dates": [], "rows": [],
                "error": result.get("message") or "No verified NSE option-chain response.",
                "http_status": result.get("http_status"), "elapsed_ms": result.get("elapsed_ms"),
                "underlying": "NIFTY",
            }
    if cache["data"] is None:
        return {
            "status": "UNAVAILABLE",
            "source": "NSE option-chain endpoint",
            "source_timestamp": None,
            "received_at": None,
            "spot": None,
            "expiry_dates": [],
            "rows": [],
            "error": "No snapshot fetched yet. Use Test connection / Fetch snapshot to request one.",
            "http_status": None,
            "underlying": "NIFTY",
        }
    data = dict(cache["data"])
    data["snapshot_age_seconds"] = max(0, int(time.time() - cache["at"]))
    return data

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

@app.get("/api/providers/indian-option-mcp/test")
def indian_option_mcp_test():
    """Run one transparent NSE connectivity test for the upstream MCP provider."""
    return test_indian_option_mcp_provider()


@app.get("/api/analysis")
def analysis(expiry: str | None = Query(default=None)):
    market_data = get_market()
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
