from __future__ import annotations
import asyncio
import math
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from io import BytesIO
from fastapi.staticfiles import StaticFiles

from providers.mcp_adapter import MCPOptionChainProvider, MCPProviderError, mcp_configuration
from services.data_quality import validate_snapshot
from services.signal_engine import analyze_snapshot, monitor_signal, ENGINE_VERSION, _tte_years
from services.greeks import calculate_greeks
from storage.database import ResearchDatabase

DB = ResearchDatabase(os.getenv('NIFTY_DATABASE_PATH', 'data/nifty_research.sqlite3'))
DB.initialize()
_PREVIOUS_VALID_SNAPSHOTS: dict[str, dict] = {}
_PROCESSED_SNAPSHOT_KEYS: set[tuple[str, str]] = set()
_SIGNAL_LOCK = threading.Lock()

def _process_snapshot(snapshot: dict, chosen_expiry: str, rows: list[dict]) -> dict:
    global _PREVIOUS_VALID_SNAPSHOTS
    source_stamp = str(snapshot.get('source_timestamp') or '')
    if not source_stamp:
        return {'direction': 'WAIT', 'decision': 'NO_TRADE', 'missing_inputs': ['source timestamp'], 'engine_version': ENGINE_VERSION}
    key = (source_stamp, chosen_expiry)
    if key in _PROCESSED_SNAPSHOT_KEYS or DB.analysis_source_seen(source_stamp, chosen_expiry):
        return {'direction': 'WAIT', 'decision': 'SNAPSHOT_ALREADY_PROCESSED', 'engine_version': ENGINE_VERSION}
    selected = dict(snapshot)
    selected['selected_expiry'] = chosen_expiry
    selected['rows'] = rows
    selected['instrument'] = 'NIFTY'
    previous = _PREVIOUS_VALID_SNAPSHOTS.get(chosen_expiry)
    decision = analyze_snapshot(selected, previous)
    _, analysis_id = DB.save_analysis(selected, decision)
    stamp = source_stamp

    # Monitor existing positions before creating a new signal from this snapshot.
    for signal in DB.get_active_signals():
        try:
            strike = float(signal['contract_key'].split('|')[2])
        except (ValueError, IndexError):
            continue
        leg = next((row.get(signal['side'].lower()) for row in rows
                    if row.get('expiry') == chosen_expiry and float(row.get('strike', -1)) == strike), None)
        if not isinstance(leg, dict):
            continue
        current_ltp = number(leg.get('lastPrice'))
        if current_ltp <= 0:
            continue
        events = DB.list_signal_events(signal['id'])
        previous_event = next((event for event in reversed(events) if event.get('observed_price') is not None), None)
        previous_price = float(previous_event['observed_price']) if previous_event else float(signal.get('actual_entry') or signal['entry_trigger'])
        previous_time = previous_event['event_timestamp'] if previous_event else (signal.get('triggered_at') or signal['created_at'])
        result = monitor_signal(signal, previous_price, current_ltp, previous_time, stamp)
        DB.append_signal_event(signal['id'], 'SNAPSHOT_OBSERVED', stamp, observed_price=current_ltp,
                               details={'source_timestamp': stamp, 'result': result})
        if result.get('outcome'):
            entry = float(signal.get('actual_entry') or signal.get('entry_trigger') or current_ltp)
            points = round(current_ltp - entry, 2)
            DB.update_signal(signal['id'], state=result['state'], outcome=result['outcome'],
                             closed_at=stamp, gross_points=points, net_points=points,
                             unresolved_reason=result.get('reason') if result['outcome'] == 'UNRESOLVED' else None)
            DB.append_signal_event(signal['id'], result['outcome'], stamp, observed_price=current_ltp,
                                   details={'reason': result.get('reason'), 'points': points})
        else:
            DB.update_signal(signal['id'], state=result.get('state', 'ACTIVE'))

    selected_candidate = decision.get('selected_candidate')
    if selected_candidate and not DB.get_active_signals():
            signal_id = DB.create_signal(analysis_id, {
                'contract_key': selected_candidate['contract_key'], 'side': selected_candidate['side'],
                'entry_trigger': selected_candidate['ltp'], 'actual_entry': selected_candidate['entry_price'],
                'target_price': selected_candidate['target_price'], 'stop_price': selected_candidate['stop_price'],
                'horizon_seconds': 900, 'state': 'ACTIVE', 'created_at': stamp,
            })
            DB.update_signal(signal_id, state='ACTIVE', triggered_at=stamp, actual_entry=selected_candidate['entry_price'])
            DB.append_signal_event(signal_id, 'ENTRY_TRIGGERED', stamp,
                observed_price=selected_candidate['entry_price'],
                details={'candidate': selected_candidate, 'engine_version': ENGINE_VERSION, 'premium_candles_required': False})
            decision['created_signal_id'] = signal_id

    _PREVIOUS_VALID_SNAPSHOTS[chosen_expiry] = selected
    _PROCESSED_SNAPSHOT_KEYS.add(key)
    if len(_PROCESSED_SNAPSHOT_KEYS) > 2000:
        _PROCESSED_SNAPSHOT_KEYS.clear()
        _PROCESSED_SNAPSHOT_KEYS.add(key)
    return decision

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
                "reason": "Preliminary liquidity and premium-momentum watchlist. Entry decisions are handled separately by the snapshot-confluence engine.",
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
    # Keep the selected expiry pinned while a signal is open so it cannot stop
    # being monitored merely because the user changes the expiry dropdown.
    active = DB.get_active_signals()
    if active:
        try:
            expiry = active[0]["contract_key"].split("|")[1]
        except (IndexError, AttributeError):
            expiry = expiry
    market_data = get_market(expiry=expiry)
    if market_data["status"] != "LIVE" or market_data.get("spot") is None:
        return {"status": "WAIT", "message": "Verified live market data is unavailable. Analysis is paused.",
                "market": market_data, "signals": [], "metrics": None, "rows": [],
                "signal_engine": {"direction": "WAIT", "decision": "NO_TRADE"}}
    available = market_data["expiry_dates"]
    chosen = expiry if expiry in available else (available[0] if available else None)
    rows = [row for row in market_data["rows"] if row["expiry"] == chosen]
    if not rows:
        return {"status": "WAIT", "message": "No contracts returned for the selected expiry.",
                "market": market_data, "signals": [], "metrics": None, "rows": []}
    metrics = expiry_analysis(rows, market_data["spot"])
    near = sorted([r for r in rows if abs(float(r["strike"]) - metrics["atm"]) <= 500],
                  key=lambda r: r["strike"])
    with _SIGNAL_LOCK:
        engine_result = _process_snapshot(market_data, chosen, rows)
    return {
        "status": "LIVE", "market": market_data, "expiry": chosen,
        "metrics": metrics, "scan_count": len(near), "rows": rows, "near_rows": near,
        "signals": rank_watchlist(near, metrics["atm"]),
        "signal_engine": engine_result, "active_signals": DB.get_active_signals(),
        "disclaimer": "Transparent snapshot-confluence rules, not a trained or validated prediction model. Premium candles are not required. Entries and outcomes are observed only from successive verified option-chain snapshots.",
    }


@app.get("/api/greeks")
def greeks(expiry: str | None = Query(default=None)):
    snapshot = get_market(expiry=expiry)
    if snapshot.get("status") != "LIVE" or snapshot.get("spot") is None:
        return {"status": "WAIT", "items": [], "message": "Verified live option-chain data is unavailable."}
    available = snapshot.get("expiry_dates", [])
    chosen = expiry if expiry in available else (available[0] if available else None)
    rows = [row for row in snapshot.get("rows", []) if row.get("expiry") == chosen]
    try:
        stamp = datetime.fromisoformat(str(snapshot.get("source_timestamp")).replace("Z", "+00:00")).astimezone(IST)
    except (ValueError, TypeError):
        return {"status": "WAIT", "items": [], "message": "Source timestamp is invalid."}
    tte = _tte_years(chosen, stamp) if chosen else None
    if not tte:
        return {"status": "WAIT", "items": [], "message": "Expiry is at or past its 15:30 IST close."}
    items = []
    for row in rows:
        for side in ("CE", "PE"):
            leg = row.get(side.lower()) or {}
            iv = number(leg.get("impliedVolatility"))
            if iv <= 0:
                continue
            try:
                result = calculate_greeks(
                    spot=float(snapshot["spot"]), strike=float(row["strike"]),
                    time_to_expiry_years=tte, implied_volatility=iv / 100.0,
                    option_type=side, risk_free_rate=float(os.getenv("RISK_FREE_RATE", "0.065")),
                    dividend_yield=0.0, iv_source="verified_option_chain_iv",
                ).as_dict()
            except (ValueError, TypeError):
                continue
            items.append({
                "strike": row["strike"], "expiry": chosen, "side": side,
                "ltp": leg.get("lastPrice"), "iv_percent": iv, **result,
            })
    return {"status": "OK", "expiry": chosen, "spot": snapshot["spot"],
            "source_timestamp": snapshot.get("source_timestamp"), "items": items}


@app.get("/api/alerts")
def alerts():
    market_data = get_market()
    if market_data["status"] != "LIVE":
        return {"status": "PAUSED", "items": DB.get_active_signals(),
                "message": "New entries paused because live market data is not verified."}
    return {"status": "MONITORING", "items": DB.get_active_signals(),
            "message": "Signals are monitored from successive verified option-chain snapshots; no premium candles are used."}


@app.get("/api/signals")
def signals(limit: int = Query(default=100, ge=1, le=500)):
    items = DB.list_signals(limit=limit)
    return {"items": items, "count": len(items)}


@app.get("/api/signals/{signal_id}/events")
def signal_events(signal_id: int):
    return {"items": DB.list_signal_events(signal_id)}


@app.get("/api/history")
def history(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    return {"items": DB.list_analyses(limit=limit, offset=offset)}


@app.get("/api/performance")
def performance():
    return DB.performance_summary()


@app.get("/api/export/signals.xlsx")
def export_signals_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    workbook = Workbook()
    signal_sheet = workbook.active
    signal_sheet.title = "Signals"
    signal_rows = DB.list_signals(limit=500)
    if signal_rows:
        headers = list(signal_rows[0].keys())
        signal_sheet.append(headers)
        for item in signal_rows:
            signal_sheet.append([item.get(key) for key in headers])
    else:
        signal_sheet.append(["No signal records yet"])
    event_sheet = workbook.create_sheet("Signal Events")
    event_sheet.append(["signal_id", "event_type", "event_timestamp", "observed_price", "details_json"])
    for item in signal_rows:
        for event in DB.list_signal_events(item["id"]):
            event_sheet.append([item["id"], event["event_type"], event["event_timestamp"],
                                event["observed_price"], event["details_json"]])
    perf_sheet = workbook.create_sheet("Performance")
    for key, value in DB.performance_summary().items():
        perf_sheet.append([key, str(value)])
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="24364B")
        for column in sheet.columns:
            width = min(42, max(12, max(len(str(cell.value or "")) for cell in column) + 2))
            sheet.column_dimensions[column[0].column_letter].width = width
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=nifty-signal-journal.xlsx"})


# Mount the owned MCP endpoint into this existing FastAPI application so the
# current Render start command (uvicorn app:app) continues to work unchanged.
from contextlib import asynccontextmanager as _asynccontextmanager
from mcp_server import mcp as _nifty_mcp, protected_mcp_app as _protected_mcp_app

_original_lifespan = app.router.lifespan_context


async def _background_market_loop():
    """Keep signal lifecycle monitoring alive even when no browser tab is open."""
    while True:
        try:
            active = DB.get_active_signals()
            expiry = None
            if active:
                try:
                    expiry = active[0]["contract_key"].split("|")[1]
                except (IndexError, AttributeError):
                    expiry = None
            snapshot = await asyncio.to_thread(lambda: get_market(force=True, expiry=expiry))
            if snapshot.get("status") == "LIVE" and snapshot.get("spot") is not None:
                chosen = expiry or snapshot.get("selected_expiry")
                if not chosen:
                    dates = snapshot.get("expiry_dates") or []
                    chosen = dates[0] if dates else None
                rows = [row for row in snapshot.get("rows", []) if row.get("expiry") == chosen]
                if chosen and rows:
                    with _SIGNAL_LOCK:
                        _process_snapshot(snapshot, chosen, rows)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A temporary provider failure must not crash the web service.
            pass
        await asyncio.sleep(max(3, min(10, int(os.getenv("MCP_POLL_INTERVAL_SECONDS", "5")))))


@_asynccontextmanager
async def _mcp_lifespan(application):
    async with _original_lifespan(application):
        async with _nifty_mcp.session_manager.run():
            poller = asyncio.create_task(_background_market_loop())
            try:
                yield
            finally:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass


app.router.lifespan_context = _mcp_lifespan
app.mount("/mcp", _protected_mcp_app(), name="mcp")
