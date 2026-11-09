"""Parse user-downloaded NSE option-chain CSV files.

This module performs no network access. Imported snapshots are labelled IMPORTED,
never LIVE, and are not eligible for automatic signal generation.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _num(value):
    if value is None:
        return None
    raw = str(value).strip().replace(",", "").replace("%", "")
    if raw in ("", "-", "--", "NA", "N/A"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_nse_option_csv(content: str, *, spot: float, expiry: str | None = None) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("CSV file is empty.")
    if spot <= 0:
        raise ValueError("A positive NIFTY spot value is required; the CSV may not contain spot.")
    rows = list(csv.reader(io.StringIO(content.lstrip("\ufeff"))))
    if len(rows) < 3:
        raise ValueError("CSV must contain a header and at least one contract row.")

    # NSE option-chain downloads commonly use two header rows: CALLS/PUTS
    # group labels followed by field names. Also accept flat normalized CSV.
    first = [_key(x) for x in rows[0]]
    second = [_key(x) for x in rows[1]] if len(rows) > 1 else []
    data_start = 2
    if any(x in ("strikeprice", "strike") for x in first):
        headers = first
        data_start = 1
    elif any(x in ("strikeprice", "strike") for x in second):
        groups, fields = first, second
        headers = []
        for i, field in enumerate(fields):
            group = groups[i] if i < len(groups) else ""
            headers.append((group + "_" + field) if group in ("calls", "puts") else field)
    else:
        headers = first
        data_start = 1

    def find_index(*names):
        wanted = {_key(x) for x in names}
        for i, name in enumerate(headers):
            if name in wanted:
                return i
        return None

    strike_i = find_index("strike price", "strikeprice", "strike")
    if strike_i is None:
        raise ValueError("Could not identify STRIKE PRICE column in this NSE CSV.")

    aliases = {
        "oi": ("calls_oi", "puts_oi", "oi", "openinterest"),
        "change_oi": ("calls_chnginoi", "puts_chnginoi", "chnginoi", "changeinopeninterest"),
        "volume": ("calls_volume", "puts_volume", "volume", "totaltradedvolume"),
        "iv": ("calls_iv", "puts_iv", "iv", "impliedvolatility"),
        "ltp": ("calls_ltp", "puts_ltp", "ltp", "lastprice"),
        "change": ("calls_chng", "puts_chng", "chng", "change"),
        "bid": ("calls_bidprice", "puts_bidprice", "bidprice"),
        "ask": ("calls_askprice", "puts_askprice", "askprice"),
        "bid_qty": ("calls_bidqty", "puts_bidqty", "bidqty"),
        "ask_qty": ("calls_askqty", "puts_askqty", "askqty"),
    }

    # Find side-specific columns; group-prefixed columns are preferred.
    indices = {}
    for side, prefix in (("ce", "calls_"), ("pe", "puts_")):
        indices[side] = {}
        for field, names in aliases.items():
            idx = next((i for i, h in enumerate(headers) if h == prefix + _key(names[0].split("_",1)[-1])), None)
            if idx is None:
                idx = next((i for i, h in enumerate(headers) if h.startswith(prefix) and h.endswith(_key(names[0].split("_",1)[-1]))), None)
            if idx is None and len(headers) and not any(h.startswith("calls_") or h.startswith("puts_") for h in headers):
                generic = {"oi":"oi","change_oi":"chnginoi","volume":"volume","iv":"iv","ltp":"ltp","change":"chng","bid":"bidprice","ask":"askprice","bid_qty":"bidqty","ask_qty":"askqty"}[field]
                idx = next((i for i,h in enumerate(headers) if h == generic), None)
            indices[side][field] = idx

    def val(cells, side, field):
        idx = indices[side][field]
        return _num(cells[idx]) if idx is not None and idx < len(cells) else None

    parsed = {}
    for cells in rows[data_start:]:
        if strike_i >= len(cells):
            continue
        strike = _num(cells[strike_i])
        if strike is None or strike <= 0:
            continue
        key = float(strike)
        record = parsed.setdefault(key, {"strike": key, "expiry": expiry or "UNSPECIFIED", "ce": {}, "pe": {}})
        for side in ("ce", "pe"):
            leg = record[side]
            mapping = {
                "openInterest": "oi", "changeinOpenInterest": "change_oi",
                "totalTradedVolume": "volume", "impliedVolatility": "iv",
                "lastPrice": "ltp", "change": "change",
                "bidprice": "bid", "askPrice": "ask",
                "bidQty": "bid_qty", "askQty": "ask_qty",
            }
            for output, field in mapping.items():
                value = val(cells, side, field)
                if value is not None:
                    leg[output] = value
    result_rows = [parsed[k] for k in sorted(parsed)]
    # Keep only strikes with both sides and actual LTP values; no price is invented.
    result_rows = [r for r in result_rows if r["ce"].get("lastPrice", 0) > 0 and r["pe"].get("lastPrice", 0) > 0]
    if not result_rows:
        raise ValueError("No complete CE/PE strike rows with valid LTP were found. Check the downloaded NSE CSV format.")
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    return {
        "instrument": "NIFTY", "status": "IMPORTED", "source": "NSE public CSV (manual upload)",
        "source_timestamp": None, "received_at": stamp, "imported_at": stamp,
        "spot": float(spot), "expiry_dates": [expiry] if expiry else [],
        "selected_expiry": expiry, "rows": result_rows,
        "quality": {"is_fresh": False, "checks": ["csv_parsed", "spot_user_supplied", "ce_pe_ltp_present"],
                    "warnings": ["Manual snapshot; exchange source timestamp is not verified.",
                                 "Imported data is not live streaming and cannot trigger entry alerts."]},
        "underlying": "NIFTY",
    }
