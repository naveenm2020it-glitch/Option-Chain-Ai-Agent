from __future__ import annotations
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from providers.mcp_adapter import MCPOptionChainProvider, MCPProviderError, mcp_configuration
from services.data_quality import validate_snapshot

ROOT = Path(__file__).parent
IST = ZoneInfo("Asia/Kolkata")
app = FastAPI(title="NIFTY Option Chain Intelligence", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

def now():
    return datetime.now(IST).isoformat(timespec="seconds")

def clean_leg(value):
    keys = ("lastPrice", "change", "pChange", "totalTradedVolume", "openInterest",
            "changeinOpenInterest", "impliedVolatility", "bidprice", "askPrice",
            "bidQty", "askQty")
    return {key: value.get(key) for key in keys}

def _fetch_market_uncached(force=False, expiry=None):
    """Fetch only through an explicitly configured MCP adapter; fail closed."""
    try:
        cfg = mcp_configuration()
        provider = MCPOptionChainProvider(cfg)
        allowed, reason = provider.configuration_status()
        if not allowed:
            return {
                "status": "UNAVAILABLE", "source": None,
                "source_timestamp": None, "received_at": now(),
                "spot": None, "expiry_dates": [], "selected_expiry": None,
                "rows": [], "underlying": "NIFTY",
                "quality": {"is_fresh": False, "checks": [], "warnings": []},
                "error": reason,
            }
        snapshot = provider.fetch_snapshot("NIFTY", expiry=expiry)
        quality = validate_snapshot(
            snapshot,
            provider_permitted=cfg["automated_use_permitted"],
            max_age_seconds=max(1, int(os.getenv("MCP_MAX_AGE_SECONDS", "30"))),
        )
        snapshot["status"] = quality.status
        snapshot["quality"] = quality.as_dict()
        snapshot["underlying"] = "NIFTY"
        if quality.status != "LIVE":
            snapshot["error"] = quality.error or "MCP snapshot failed data-quality validation."
        else:
            snapshot.pop("error", None)
        return snapshot
    except (MCPProviderError, ValueError) as exc:
        return {
            "status": "UNAVAILABLE", "source": "MCP",
            "source_timestamp": None, "received_at": now(),
            "spot": None, "expiry_dates": [], "selected_expiry": None,
            "rows": [], "underlying": "NIFTY",
            "quality": {"is_fresh": False, "checks": [], "warnings": []},
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "status": "UNAVAILABLE", "source": "MCP",
            "source_timestamp": None, "received_at": now(),
            "spot": None, "expiry_dates": [], "selected_expiry": None,
            "rows": [], "underlying": "NIFTY",
            "quality": {"is_fresh": False, "checks": [], "warnings": []},
            "error": f"MCP provider failure: {type(exc).__name__}",
        }

_MCP_MARKET_CACHE: dict[str, tuple[float, dict]] = {}
_MCP_MARKET_CACHE_LOCK = threading.Lock()


def get_market(force=False, expiry=None):
    """Share one bounded backend poll across dashboard/API requests."""
    key = expiry or "__nearest__"
    ttl = max(1, min(30, int(os.getenv("MCP_POLL_INTERVAL_SECONDS", "5"))))
    with _MCP_MARKET_CACHE_LOCK:
        cached = _MCP_MARKET_CACHE.get(key)
        if not force and cached and (time.monotonic() - cached[0]) < ttl:
            return cached[1]
        snapshot = _fetch_market_uncached(force=force, expiry=expiry)
        _MCP_MARKET_CACHE[key] = (time.monotonic(), snapshot)
        return snapshot


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
                "reason": "Rules-based option-chain screen only. Premium candles are not required. This is a WATCH-only screen; the option-chain signal lifecycle is still pending.",
            })
    return sorted(result, key=lambda item: item["score"], reverse=True)[:12]

@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")

@app.get("/api/health")
def health():
    # Health probes must not trigger upstream market-data calls.
    try:
        cfg = mcp_configuration()
        configured = bool(cfg["enabled"] and cfg["automated_use_permitted"] and cfg["command"] and cfg["tool_name"])
    except MCPProviderError:
        configured = False
    return {
        "ok": True,
        "time": now(),
        "data_status": "CONFIGURED_NOT_CHECKED" if configured else "UNAVAILABLE",
        "provider": "MCP" if configured else "none",
        "provider_configured": configured,
        "provider_status": "CONFIGURED_NOT_CHECKED" if configured else "UNAVAILABLE",
    }

@app.get("/api/market")
def market(force: bool = False, expiry: str | None = Query(default=None)):
    return get_market(force=force, expiry=expiry)

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
        "disclaimer": "Screening heuristic, not a trained or validated prediction model. Premium candles are not required; the option-chain entry lifecycle engine is not yet implemented.",
    }

@app.get("/api/alerts")
def alerts():
    market_data = get_market()
    if market_data["status"] != "LIVE":
        return {"status": "PAUSED", "items": [], "message": "Alerts paused because live market data is not verified."}
    return {"status": "MONITORING", "items": [],
            "message": "No confirmed entry alerts. The option-chain signal lifecycle engine is not yet implemented. No entry alerts are generated."}
