"""Safe connectivity test for the NSE endpoint used by Indian-Option-MCP.

This is deliberately a single-attempt diagnostic adapter. It does not rotate
user agents, retry blocked requests, or attempt to evade NSE access controls.
It reports the HTTP result and the exchange timestamp exactly as received.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
NSE_HOME = "https://www.nseindia.com"
NSE_CHAIN = f"{NSE_HOME}/api/option-chain-indices?symbol=NIFTY"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NIFTY-LAB-Research/1.0)",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": f"{NSE_HOME}/option-chain",
}


def test_provider() -> dict:
    started = time.perf_counter()
    received_at = datetime.now(IST).isoformat(timespec="seconds")
    result = {
        "provider": "Indian-Option-MCP NSE endpoint compatibility test",
        "provider_repository": "https://github.com/devag7/Indian-Option-MCP",
        "transport": "Direct NSE HTTPS request (not the MCP stdio transport)",
        "endpoint": NSE_CHAIN,
        "status": "UNAVAILABLE",
        "http_status": None,
        "exchange_timestamp": None,
        "received_at": received_at,
        "spot": None,
        "expiry_count": 0,
        "strike_rows": 0,
        "ce_rows": 0,
        "pe_rows": 0,
        "elapsed_ms": None,
        "message": "",
    }

    session = requests.Session()
    try:
        # One ordinary session handshake, followed by one option-chain request.
        session.get(NSE_HOME, headers=HEADERS, timeout=8)
        response = session.get(NSE_CHAIN, headers=HEADERS, timeout=10)
        result["http_status"] = response.status_code
        if response.status_code == 403:
            result["message"] = (
                "NSE returned HTTP 403. The endpoint is inaccessible from this "
                "server at the moment. No retry, user-agent rotation, or bypass was attempted."
            )
            return _finish(result, started)
        response.raise_for_status()

        payload = response.json()
        records = payload.get("records") or {}
        rows = records.get("data") or []
        ce_count = sum(1 for row in rows if isinstance(row.get("CE"), dict))
        pe_count = sum(1 for row in rows if isinstance(row.get("PE"), dict))
        if not rows:
            result["status"] = "EMPTY"
            result["message"] = "NSE responded successfully but returned no option-chain rows."
            return _finish(result, started)

        expiry_dates = records.get("expiryDates") or []
        normalized = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            strike = item.get("strikePrice")
            if strike is None:
                continue
            normalized.append({
                "strike": strike,
                "expiry": item.get("expiryDate"),
                "ce": item.get("CE") if isinstance(item.get("CE"), dict) else {},
                "pe": item.get("PE") if isinstance(item.get("PE"), dict) else {},
            })
        result.update({
            "status": "CONNECTED",
            "exchange_timestamp": records.get("timestamp") or payload.get("timestamp"),
            "spot": records.get("underlyingValue"),
            "expiry_dates": expiry_dates,
            "expiry_count": len(expiry_dates),
            "strike_rows": len(normalized),
            "ce_rows": ce_count,
            "pe_rows": pe_count,
            "rows": normalized,
            "message": (
                "NSE returned a genuine option-chain JSON snapshot. This is a one-time user-triggered "
                "fetch; it does not establish an authorised data licence or guarantee availability."
            ),
        })
        return _finish(result, started)
    except requests.HTTPError as exc:
        result["message"] = f"NSE HTTP request failed: {exc}"
        return _finish(result, started)
    except requests.RequestException as exc:
        result["message"] = f"Network request failed: {exc}"
        return _finish(result, started)
    except (ValueError, TypeError) as exc:
        result["message"] = f"NSE response could not be validated as option-chain JSON: {exc}"
        return _finish(result, started)
    except Exception as exc:
        result["message"] = f"Provider test failed: {exc}"
        return _finish(result, started)
    finally:
        session.close()


def _finish(result: dict, started: float) -> dict:
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return result
