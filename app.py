from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score
except Exception:  # ML stays unavailable rather than blocking report processing.
    RandomForestClassifier = None
    accuracy_score = None


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("NIFTY_DATA_DIR", APP_DIR / "data")).resolve()
INCOMING_DIR = Path(os.getenv("NIFTY_INCOMING_DIR", APP_DIR / "INCOMING_REPORTS")).resolve()
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "nifty_intelligence.sqlite3"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "80")) * 1024 * 1024
REPORT_TYPES = {
    "nifty50": "NIFTY 50 Historical Index",
    "india_vix": "India VIX Historical Index",
    "cm_bhavcopy": "CM UDiFF Common Bhavcopy",
    "cm_delivery": "Security-wise Delivery Positions",
    "cm_volatility": "CM Daily Volatility",
    "fii_dii": "FII/FPI & DII Activity",
    "fo_bhavcopy": "F&O UDiFF Common Bhavcopy",
    "participant_oi": "Participant-wise Open Interest",
    "participant_volume": "Participant-wise Trading Volumes",
    "fii_derivatives": "FII Derivatives Statistics",
    "fo_settlement": "F&O Daily Settlement Prices",
    "fo_volatility": "F&O Daily Volatility",
}
REQUIRED_TYPES = list(REPORT_TYPES.keys())
app = FastAPI(title="NIFTY Next-Day AI Intelligence", version="1.0.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
scan_lock = threading.Lock()
scan_state = {"enabled": True, "last_scan": None, "last_error": None, "seen": set(), "last_size": {}}


def now_ist() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            report_type TEXT NOT NULL,
            trade_date TEXT,
            sha256 TEXT NOT NULL UNIQUE,
            bytes INTEGER NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS prediction_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            levels_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        con.commit()


def db():
    import sqlite3
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def clean_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:180] or "report"


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def classify_report(filename: str, headers: list[str] | None = None) -> str:
    n = norm(filename)
    h = " ".join(norm(x) for x in (headers or []))
    combined = n + " " + h
    rules = [
        ("participant_oi", ["participantoi", "participantwiseopeninterest", "fao_participant_oi"]),
        ("participant_volume", ["participantvol", "participantwisetradingvolume", "fao_participant_vol"]),
        ("fii_derivatives", ["fiiderivative", "fiistats", "fiiderivativesstatistics"]),
        ("fo_settlement", ["fosett", "settlementprice", "fo_dailysettlement"]),
        ("fo_volatility", ["fovolt", "fovolatility", "fodailyvolatility"]),
        ("cm_volatility", ["cmvolt", "cmvolatility", "cmdailyvolatility"]),
        ("cm_delivery", ["deliveryposition", "deliverabledata", "securitywisedelivery", "mto_"]),
        ("fii_dii", ["fiidii", "fii", "dii", "institutionalactivity"]),
        ("fo_bhavcopy", ["fo bhav", "fobhav", "bhavcopyfo", "nsefo", "fo_udiff", "fao"]),
        ("cm_bhavcopy", ["cm bhav", "cmbhav", "bhavcopycm", "nsecash", "nsecm", "cm_udiff"]),
        ("india_vix", ["indiavix", "vix"]),
        ("nifty50", ["nifty50", "nifty_50", "niftyindex", "historicalindex"]),
    ]
    for report_type, words in rules:
        if any(norm(word) in combined for word in words):
            if report_type == "fii_dii" and "derivative" in combined:
                continue
            return report_type
    if "strkpric" in h and ("optn" in h or "openinterest" in h or "opnintrst" in h):
        return "fo_bhavcopy"
    if "indexname" in h and ("closingindexvalue" in h or "close" in h):
        if "vix" in combined:
            return "india_vix"
        return "nifty50"
    if "security" in h and ("deliverableqty" in h or "delivery" in h):
        return "cm_delivery"
    if "participant" in h and "openinterest" in h:
        return "participant_oi"
    return "unclassified"


def extract_date(filename: str) -> str | None:
    name = filename.upper()
    patterns = [
        r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})",
        r"(\d{2})[-_]?([A-Z]{3})[-_]?(\d{4})",
        r"(\d{2})(\d{2})(20\d{2})",
    ]
    for p in patterns:
        m = re.search(p, name)
        if not m:
            continue
        try:
            if p.startswith("(20"):
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            if m.group(2).isalpha():
                return datetime.strptime(" ".join(m.groups()), "%d %b %Y").date().isoformat()
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except Exception:
            pass
    return None


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def read_table_bytes(filename: str, raw: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in (".xls", ".xlsx"):
        return pd.read_excel(io.BytesIO(raw), dtype=str)
    text = decode_bytes(raw)
    first_lines = text.splitlines()[:8]
    delimiter = "," if sum(x.count(",") for x in first_lines) >= sum(x.count("|") for x in first_lines) else "|"
    if suffix in (".dat", ".txt") and delimiter == "," and any("\t" in x for x in first_lines):
        delimiter = "\t"
    try:
        frame = pd.read_csv(io.StringIO(text), sep=delimiter, dtype=str, engine="python", on_bad_lines="skip")
        if frame.shape[1] <= 1 and delimiter != ",":
            frame = pd.read_csv(io.StringIO(text), dtype=str, engine="python", on_bad_lines="skip")
        return frame.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def files_from_payload(filename: str, raw: bytes):
    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for item in archive.infolist():
                    if item.is_dir() or item.file_size > MAX_UPLOAD_BYTES:
                        continue
                    lower = item.filename.lower()
                    if lower.endswith((".csv", ".dat", ".txt", ".xls", ".xlsx")):
                        yield Path(item.filename).name, archive.read(item)
        except zipfile.BadZipFile:
            raise ValueError("The ZIP file is damaged or incomplete.")
    else:
        yield filename, raw


def get_col(df: pd.DataFrame, aliases: list[str]) -> str | None:
    lookup = {norm(c): c for c in df.columns}
    for alias in aliases:
        key = norm(alias)
        if key in lookup:
            return lookup[key]
    for alias in aliases:
        key = norm(alias)
        if len(key) < 3:
            continue
        for existing, original in lookup.items():
            if key and (key in existing or existing in key):
                return original
    return None


def num(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype=float)
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False).str.strip(), errors="coerce")


def latest_rows(df: pd.DataFrame, date_aliases: list[str]) -> pd.DataFrame:
    col = get_col(df, date_aliases)
    if not col:
        return df
    parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
    if parsed.notna().any():
        latest = parsed.max()
        return df.loc[parsed == latest]
    return df


def infer_trade_date(report_type: str, df: pd.DataFrame, fallback: str | None) -> str | None:
    date_col = get_col(df, ["Index Date", "Date", "TradDt", "BizDt", "Trade Date", "TDate", "TIMESTAMP"])
    if date_col:
        parsed = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True).dropna()
        if not parsed.empty:
            return parsed.max().date().isoformat()
    return fallback


def table_from_file(path: Path, report_type: str):
    if not path.exists():
        return []
    if path.suffix.lower() == ".zip":
        tables = []
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith((".csv", ".dat", ".txt", ".xls", ".xlsx")):
                        continue
                    raw = archive.read(info)
                    frame = read_table_bytes(info.filename, raw)
                    detected = classify_report(info.filename, list(frame.columns))
                    if detected == "unclassified":
                        detected = report_type
                    tables.append((Path(info.filename).name, detected, frame))
        except Exception:
            return []
        return tables
    try:
        return [(path.name, report_type, read_table_bytes(path.name, path.read_bytes()))]
    except Exception:
        return []


def store_report(filename: str, raw: bytes, forced_type: str | None = None, source: str = "upload") -> dict:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{filename} exceeds the upload size limit.")
    sha = hashlib.sha256(raw).hexdigest()
    with db() as con:
        existing = con.execute("SELECT id,file_name,report_type,trade_date,row_count,status FROM reports WHERE sha256=?", (sha,)).fetchone()
        if existing:
            return {**dict(existing), "duplicate": True}
    safe = clean_name(filename)
    base_type = forced_type if forced_type in REPORT_TYPES else classify_report(safe)
    guessed_date = extract_date(safe)
    folder = RAW_DIR / (guessed_date or datetime.now().date().isoformat())
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / safe
    if saved.exists():
        saved = folder / (saved.stem + "_" + sha[:8] + saved.suffix)
    saved.write_bytes(raw)
    frames = list(files_from_payload(safe, raw))
    total_rows = 0
    detected_types = []
    notes = []
    actual_dates = []
    for inner_name, inner_raw in frames:
        df = read_table_bytes(inner_name, inner_raw)
        detected = forced_type if forced_type in REPORT_TYPES else classify_report(inner_name, list(df.columns))
        if detected == "unclassified":
            detected = base_type
        if detected != "unclassified":
            detected_types.append(detected)
        total_rows += int(len(df.index))
        dt = infer_trade_date(detected, df, extract_date(inner_name) or guessed_date)
        if dt:
            actual_dates.append(dt)
        if df.empty:
            notes.append(f"No tabular rows detected in {inner_name}")
    if not detected_types and base_type != "unclassified":
        detected_types = [base_type]
    if forced_type in REPORT_TYPES:
        base_type = forced_type
    elif base_type == "unclassified" and detected_types:
        base_type = detected_types[0]
    elif base_type == "unclassified" and detected_types:
        base_type = detected_types[0]
    elif base_type == "unclassified":
        base_type = "unclassified"
    trade_date = max(actual_dates) if actual_dates else guessed_date
    status = "parsed" if total_rows > 0 and base_type != "unclassified" else ("needs_review" if total_rows > 0 else "parse_error")
    note = "; ".join(notes + ([] if base_type != "unclassified" else ["Report type not recognized; assign it manually in Data Centre."]))
    with db() as con:
        cur = con.execute("""INSERT INTO reports(file_name,stored_path,report_type,trade_date,sha256,bytes,row_count,status,notes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (safe, str(saved), base_type, trade_date, sha, len(raw), total_rows, status, note, now_ist()))
        con.commit()
        rid = cur.lastrowid
    return {"id": rid, "file_name": safe, "report_type": base_type, "report_label": REPORT_TYPES.get(base_type, "Needs classification"),
            "trade_date": trade_date, "bytes": len(raw), "row_count": total_rows, "status": status, "notes": note, "duplicate": False}


def load_reports() -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT * FROM reports ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def report_tables(trade_date: str | None = None):
    rows = load_reports()
    grouped = {}
    for row in rows:
        if row["status"] == "duplicate":
            continue
        if trade_date and row["trade_date"] and row["trade_date"] != trade_date:
            continue
        key = row["report_type"]
        if key == "unclassified":
            continue
        if key not in grouped:
            grouped[key] = table_from_file(Path(row["stored_path"]), key)
    return grouped, rows


def index_analysis(tables: list) -> dict:
    if not tables:
        return {"status": "insufficient", "reason": "NIFTY 50 historical index report is missing."}
    _, _, df = tables[0]
    if df.empty:
        return {"status": "insufficient", "reason": "NIFTY 50 index report has no readable rows."}
    high_c = get_col(df, ["High Index Value", "High", "High Price", "H"])
    low_c = get_col(df, ["Low Index Value", "Low", "Low Price", "L"])
    close_c = get_col(df, ["Closing Index Value", "Close Index Value", "Close", "Closing Price", "ClsPric", "C"])
    open_c = get_col(df, ["Open Index Value", "Open", "Open Price", "OpnPric", "O"])
    date_c = get_col(df, ["Index Date", "Date", "TradDt", "Trade Date", "TIMESTAMP"])
    if not all([high_c, low_c, close_c]):
        return {"status": "insufficient", "reason": "Could not identify High, Low and Close columns in the NIFTY 50 report.",
                "available_columns": [str(c) for c in df.columns]}
    frame = df.copy()
    frame["_h"] = num(frame[high_c]); frame["_l"] = num(frame[low_c]); frame["_c"] = num(frame[close_c])
    frame["_o"] = num(frame[open_c]) if open_c else frame["_c"]
    frame = frame.dropna(subset=["_h", "_l", "_c"])
    if date_c:
        frame["_date_sort"] = pd.to_datetime(frame[date_c], errors="coerce", dayfirst=True)
        frame = frame.sort_values("_date_sort", na_position="last")
    else:
        frame = frame.sort_index()
    if frame.empty:
        return {"status": "insufficient", "reason": "NIFTY 50 OHLC values are not numeric."}
    last = frame.iloc[-1]
    prev = frame.iloc[-2] if len(frame) > 1 else None
    high, low, close, opn = map(float, (last["_h"], last["_l"], last["_c"], last["_o"]))
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * pivot - bc
    levels = {
        "PDH": high, "PDL": low, "PDC": close, "PIVOT": pivot,
        "R1": 2 * pivot - low, "S1": 2 * pivot - high,
        "R2": pivot + (high - low), "S2": pivot - (high - low),
        "R3": high + 2 * (pivot - low), "S3": low - 2 * (high - pivot),
        "CPR_BC": min(bc, tc), "CPR_TC": max(bc, tc),
    }
    change = (close - float(prev["_c"])) if prev is not None else None
    change_pct = (change / float(prev["_c"]) * 100) if prev is not None and float(prev["_c"]) else None
    date_value = str(last[date_c])[:20] if date_c else None
    return {"status": "valid", "date": date_value, "open": opn, "high": high, "low": low, "close": close,
            "previous_close": float(prev["_c"]) if prev is not None else None, "change": change, "change_pct": change_pct,
            "rows": len(frame), "levels": levels, "history": [{"date": str(row[date_c])[:20] if date_c else str(i), "open": float(row["_o"]), "high": float(row["_h"]), "low": float(row["_l"]), "close": float(row["_c"])} for i, row in frame.tail(120).iterrows()]}


def vix_analysis(tables: list) -> dict:
    if not tables:
        return {"status": "missing"}
    _, _, df = tables[0]
    close_c = get_col(df, ["Closing Index Value", "Close", "Close Price", "India VIX", "ClsPric"])
    if not close_c:
        return {"status": "needs_review", "columns": list(map(str, df.columns))}
    frame = df.copy()
    date_c = get_col(frame, ["Index Date", "Date", "Trade Date", "TIMESTAMP", "TradDt"])
    if date_c:
        frame["_date_sort"] = pd.to_datetime(frame[date_c], errors="coerce", dayfirst=True)
        frame = frame.sort_values("_date_sort", na_position="last")
    series = num(frame[close_c]).dropna()
    if series.empty:
        return {"status": "insufficient"}
    value = float(series.iloc[-1])
    prior = float(series.iloc[-2]) if len(series) > 1 else None
    return {"status": "valid", "close": value, "previous_close": prior, "change": value - prior if prior is not None else None}


def options_analysis(tables: list) -> dict:
    if not tables:
        return {"status": "missing", "reason": "F&O Bhavcopy is not imported."}
    _, _, df = tables[0]
    symbol_c = get_col(df, ["TckrSymb", "Symbol", "Underlying", "underlying_symbol"])
    strike_c = get_col(df, ["StrkPric", "Strike Price", "Strike"])
    opt_c = get_col(df, ["OptnTp", "Option Type", "OptionType", "CP"])
    oi_c = get_col(df, ["OpnIntrst", "Open Interest", "OI"])
    vol_c = get_col(df, ["TtlTradgVol", "Total Traded Volume", "Volume"])
    ltp_c = get_col(df, ["ClsPric", "Close Price", "Last Price", "LTP"])
    expiry_c = get_col(df, ["XpryDt", "Expiry Date", "Expiry"])
    if not all([strike_c, opt_c, oi_c]):
        return {"status": "needs_review", "reason": "Could not map strike, option type and open-interest columns.",
                "available_columns": [str(c) for c in df.columns]}
    work = df.copy()
    if symbol_c:
        work = work[work[symbol_c].astype(str).str.upper().str.contains("NIFTY", na=False)]
    if expiry_c:
        expiry_values = pd.to_datetime(work[expiry_c], errors="coerce", dayfirst=True)
        valid_expiries = expiry_values.dropna()
        if not valid_expiries.empty:
            trade_col = get_col(work, ["TradDt", "Trade Date", "Date"])
            session_date = pd.to_datetime(work[trade_col], errors="coerce", dayfirst=True).max() if trade_col else pd.Timestamp.now().normalize()
            future_expiries = valid_expiries[valid_expiries >= session_date.normalize()]
            selected_expiry = future_expiries.min() if not future_expiries.empty else valid_expiries.max()
            work = work.loc[expiry_values == selected_expiry]
    work["_strike"] = num(work[strike_c]); work["_oi"] = num(work[oi_c]).fillna(0)
    work["_vol"] = num(work[vol_c]).fillna(0) if vol_c else 0
    work["_ltp"] = num(work[ltp_c]) if ltp_c else np.nan
    work["_opt"] = work[opt_c].astype(str).str.upper().str.strip()
    calls = work[work["_opt"].isin(["CE", "C", "CALL"])]
    puts = work[work["_opt"].isin(["PE", "P", "PUT"])]
    if calls.empty and puts.empty:
        return {"status": "insufficient", "reason": "No NIFTY CE/PE rows found for the selected expiry."}
    ce_oi, pe_oi = float(calls["_oi"].sum()), float(puts["_oi"].sum())
    pcr = pe_oi / ce_oi if ce_oi else None
    ce_by = calls.groupby("_strike")["_oi"].sum().sort_values(ascending=False)
    pe_by = puts.groupby("_strike")["_oi"].sum().sort_values(ascending=False)
    strikes = sorted(set(work["_strike"].dropna().astype(float).tolist()))
    max_pain = None
    if strikes:
        payouts = []
        for settle in strikes:
            call_payout = sum(max(0, settle - float(s)) * float(oi) for s, oi in calls.groupby("_strike")["_oi"].sum().items())
            put_payout = sum(max(0, float(s) - settle) * float(oi) for s, oi in puts.groupby("_strike")["_oi"].sum().items())
            payouts.append((call_payout + put_payout, settle))
        max_pain = min(payouts)[1]
    expiry = str(work[expiry_c].iloc[0]) if expiry_c and not work.empty else None
    return {"status": "valid", "expiry": expiry, "pcr": pcr, "ce_total_oi": ce_oi, "pe_total_oi": pe_oi,
            "max_pain": max_pain, "call_resistance_candidates": [{"strike": float(k), "oi": float(v)} for k, v in ce_by.head(5).items()],
            "put_support_candidates": [{"strike": float(k), "oi": float(v)} for k, v in pe_by.head(5).items()],
            "rows": int(len(work)), "volume_total": float(work["_vol"].sum()) if hasattr(work["_vol"], "sum") else 0,
            "note": "OI concentrations and Max Pain are positioning references, not guaranteed support or resistance."}


def breadth_analysis(tables: list) -> dict:
    if not tables:
        return {"status": "missing"}
    _, _, df = tables[0]
    close_c = get_col(df, ["ClsPric", "Close Price", "Close", "Closing Price"])
    prev_c = get_col(df, ["PrvsClsgPric", "Previous Close", "Prev Close", "Previous Closing Price"])
    if not close_c or not prev_c:
        return {"status": "needs_review", "available_columns": [str(c) for c in df.columns]}
    close, prev = num(df[close_c]), num(df[prev_c])
    valid = close.notna() & prev.notna() & (prev != 0)
    diff = close[valid] - prev[valid]
    return {"status": "valid", "advances": int((diff > 0).sum()), "declines": int((diff < 0).sum()),
            "unchanged": int((diff == 0).sum()), "securities": int(valid.sum())}


def institutional_analysis(fii_tables: list, participant_oi: list, fii_stats: list) -> dict:
    result = {"status": "partial", "net_activity": None, "participant_files": bool(participant_oi), "fii_statistics": bool(fii_stats), "evidence": []}
    if fii_tables:
        _, _, df = fii_tables[0]
        net_c = get_col(df, ["Net Value", "NetValue", "Net Amount", "Net"])
        category_c = get_col(df, ["Category", "Client Type", "Investor Type", "Participant"])
        if net_c:
            latest = latest_rows(df, ["Date", "Trade Date", "TIMESTAMP"])
            values = num(latest[net_c]).dropna()
            if not values.empty:
                result["net_activity"] = float(values.sum())
                result["status"] = "valid"
                result["evidence"].append({"source": "FII/DII activity", "net_value": result["net_activity"], "category_column": category_c})
    return result


def volatility_analysis(vix: dict, cm_tables: list, fo_tables: list) -> dict:
    result = {"status": "partial", "india_vix": vix.get("close"), "india_vix_change": vix.get("change"), "cash_report": bool(cm_tables), "fo_report": bool(fo_tables)}
    if vix.get("status") == "valid":
        result["status"] = "valid"
        result["regime"] = "elevated" if vix["close"] >= 20 else ("moderate" if vix["close"] >= 13 else "compressed")
    return result


def ml_pattern_agent(index: dict) -> dict:
    history = index.get("history", [])
    if len(history) < 120:
        return {"agent": "ML Pattern Engine", "status": "insufficient_history", "bias": "UNAVAILABLE",
                "evidence": [f"Need at least 120 valid NIFTY 50 daily observations; received {len(history)}."],
                "model": "Random Forest (planned when sufficient history exists)", "accuracy": None}
    if RandomForestClassifier is None:
        return {"agent": "ML Pattern Engine", "status": "unavailable", "bias": "UNAVAILABLE", "evidence": ["scikit-learn is not installed."]}
    frame = pd.DataFrame(history)
    frame["range_pct"] = (frame["high"] - frame["low"]) / frame["close"].replace(0, np.nan) * 100
    frame["body_pct"] = (frame["close"] - frame["open"]) / frame["open"].replace(0, np.nan) * 100
    frame["close_pos"] = (frame["close"] - frame["low"]) / (frame["high"] - frame["low"]).replace(0, np.nan)
    frame["return_pct"] = frame["close"].pct_change() * 100
    next_close = frame["close"].shift(-1)
    frame["target"] = np.where(next_close.notna(), (next_close > frame["close"]).astype(float), np.nan)
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna()
    features = ["return_pct", "range_pct", "body_pct", "close_pos"]
    if len(frame) < 100:
        return {"agent": "ML Pattern Engine", "status": "insufficient_history", "bias": "UNAVAILABLE", "evidence": ["Not enough clean training observations."]}
    split = max(60, int(len(frame) * 0.8))
    split = min(split, len(frame) - 20)
    model = RandomForestClassifier(n_estimators=120, max_depth=4, min_samples_leaf=4, random_state=42, class_weight="balanced")
    model.fit(frame[features].iloc[:split], frame["target"].iloc[:split])
    accuracy = float(accuracy_score(frame["target"].iloc[split:], model.predict(frame[features].iloc[split:]))) if accuracy_score else None
    model.fit(frame[features], frame["target"])
    latest = frame[features].iloc[[-1]]
    probability_up = float(model.predict_proba(latest)[0][list(model.classes_).index(1)])
    bias = "BULLISH" if probability_up >= 0.56 else ("BEARISH" if probability_up <= 0.44 else "NEUTRAL")
    return {"agent": "ML Pattern Engine", "status": "validated_sample", "bias": bias,
            "probability_up": round(probability_up, 4), "holdout_accuracy": round(accuracy, 4) if accuracy is not None else None,
            "holdout_samples": int(len(frame) - split), "evidence": [f"Chronological holdout observations: {len(frame)-split}.", "Probability is model output, not a guarantee."]}


def build_analysis(trade_date: str | None = None) -> dict:
    grouped, reports = report_tables(trade_date)
    selected_date = trade_date or max([r["trade_date"] for r in reports if r["trade_date"]] or [datetime.now().date().isoformat()])
    index = index_analysis(grouped.get("nifty50", []))
    vix = vix_analysis(grouped.get("india_vix", []))
    options = options_analysis(grouped.get("fo_bhavcopy", []))
    breadth = breadth_analysis(grouped.get("cm_bhavcopy", []))
    institutional = institutional_analysis(grouped.get("fii_dii", []), grouped.get("participant_oi", []), grouped.get("fii_derivatives", []))
    volatility = volatility_analysis(vix, grouped.get("cm_volatility", []), grouped.get("fo_volatility", []))
    ml = ml_pattern_agent(index)
    price_bias = "UNAVAILABLE"
    if index.get("status") == "valid" and index.get("change_pct") is not None:
        price_bias = "BULLISH" if index["change_pct"] > 0.15 else ("BEARISH" if index["change_pct"] < -0.15 else "NEUTRAL")
    option_bias = "UNAVAILABLE"
    if options.get("status") == "valid" and options.get("pcr") is not None:
        option_bias = "BULLISH" if options["pcr"] >= 1.15 else ("BEARISH" if options["pcr"] <= 0.85 else "NEUTRAL")
    breadth_bias = "UNAVAILABLE"
    if breadth.get("status") == "valid":
        ratio = breadth["advances"] - breadth["declines"]
        breadth_bias = "BULLISH" if ratio > 0 else ("BEARISH" if ratio < 0 else "NEUTRAL")
    agents = [
        {"agent": "Price Action", "status": index.get("status"), "bias": price_bias,
         "evidence": [f"NIFTY close: {index.get('close')}", f"Previous-session change: {index.get('change_pct')}%"] if index.get("status") == "valid" else [index.get("reason", "NIFTY OHLC unavailable.")],
         "details": index},
        {"agent": "Options Intelligence", "status": options.get("status"), "bias": option_bias,
         "evidence": [f"PCR: {options.get('pcr')}", f"Max Pain: {options.get('max_pain')}"] if options.get("status") == "valid" else [options.get("reason", "F&O data unavailable.")],
         "details": options},
        {"agent": "Institutional Flow", "status": institutional.get("status"), "bias": "UNAVAILABLE",
         "evidence": institutional.get("evidence", []) or ["Participant and FII/DII reports imported; no validated directional mapping yet."], "details": institutional},
        {"agent": "Market Breadth", "status": breadth.get("status"), "bias": breadth_bias,
         "evidence": [f"Advances: {breadth.get('advances')}", f"Declines: {breadth.get('declines')}"] if breadth.get("status") == "valid" else ["Breadth columns not mapped or data missing."], "details": breadth},
        {"agent": "Volatility", "status": volatility.get("status"), "bias": "NEUTRAL" if volatility.get("status") == "valid" else "UNAVAILABLE",
         "evidence": [f"India VIX: {volatility.get('india_vix')}", f"Volatility regime: {volatility.get('regime', 'not classified')}"], "details": volatility},
        ml,
    ]
    available = [a for a in agents if a.get("bias") in ("BULLISH", "BEARISH", "NEUTRAL")]
    score = sum(1 if a["bias"] == "BULLISH" else -1 if a["bias"] == "BEARISH" else 0 for a in available)
    sentiment = "INSUFFICIENT DATA"
    if index.get("status") == "valid":
        sentiment = "BULLISH" if score >= 2 else ("BEARISH" if score <= -2 else "NEUTRAL / MIXED")
    required_available = [r for r in reports if r["report_type"] in REQUIRED_TYPES and r["status"] in ("parsed", "needs_review")]
    coverage = len(set(r["report_type"] for r in required_available))
    levels = index.get("levels", {})
    result = {
        "trade_date": selected_date, "generated_at": now_ist(), "sentiment": sentiment,
        "sentiment_note": "Rule-based evidence blend. Directional probabilities remain uncalibrated until sufficient walk-forward history is available.",
        "coverage": {"received": coverage, "required": len(REQUIRED_TYPES), "complete": coverage == len(REQUIRED_TYPES)},
        "levels": levels, "index": index, "india_vix": vix, "options": options, "agents": agents,
        "reports": [{"id": r["id"], "file_name": r["file_name"], "report_type": r["report_type"],
                     "report_label": REPORT_TYPES.get(r["report_type"], "Unclassified"), "trade_date": r["trade_date"],
                     "row_count": r["row_count"], "status": r["status"], "notes": r["notes"]} for r in reports],
        "quality_flags": ([f"Only {coverage} of {len(REQUIRED_TYPES)} report categories are present."] if coverage < len(REQUIRED_TYPES) else [])
                         + (["NIFTY 50 OHLC unavailable; no support/resistance forecast can be published."] if index.get("status") != "valid" else [])
                         + (["ML pattern model has insufficient history or is unavailable."] if ml.get("status") != "validated_sample" else []),
    }
    return result


def save_analysis(result: dict) -> int:
    with db() as con:
        cur = con.execute("INSERT INTO analyses(trade_date,result_json,created_at) VALUES(?,?,?)",
                          (result["trade_date"], json.dumps(result, default=str), now_ist()))
        if result.get("levels"):
            con.execute("INSERT INTO prediction_history(session_date,sentiment,levels_json,result_json,created_at) VALUES(?,?,?,?,?)",
                        (result["trade_date"], result["sentiment"], json.dumps(result["levels"]), json.dumps(result, default=str), now_ist()))
        con.commit()
        return int(cur.lastrowid)


def scan_incoming_folder():
    if not scan_lock.acquire(blocking=False):
        return
    try:
        INCOMING_DIR.mkdir(parents=True, exist_ok=True)
        for path in INCOMING_DIR.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
                if stat.st_size <= 0 or time.time() - stat.st_mtime < 2:
                    continue
                marker = str(path.resolve())
                if scan_state["last_size"].get(marker) != stat.st_size:
                    scan_state["last_size"][marker] = stat.st_size
                    continue
                raw = path.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                if sha in scan_state["seen"]:
                    continue
                saved = store_report(path.name, raw, source="folder")
                scan_state["seen"].add(sha)
            except Exception as exc:
                scan_state["last_error"] = str(exc)
        scan_state["last_scan"] = now_ist()
        scan_state["last_error"] = None
    except Exception as exc:
        scan_state["last_error"] = str(exc)
    finally:
        scan_lock.release()


def folder_watcher():
    while True:
        scan_incoming_folder()
        time.sleep(5)


@app.on_event("startup")
def startup():
    ensure_storage()
    if os.getenv("DISABLE_FOLDER_WATCHER", "false").lower() != "true":
        thread = threading.Thread(target=folder_watcher, daemon=True, name="nifty-folder-watcher")
        thread.start()


@app.get("/")
def home():
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/api/health")
def health():
    ensure_storage()
    return {"status": "ok", "app": "NIFTY Next-Day AI Intelligence", "version": app.version,
            "mode": "local" if os.getenv("RENDER") is None else "render",
            "incoming_folder": str(INCOMING_DIR), "data_directory": str(DATA_DIR),
            "folder_watcher": os.getenv("DISABLE_FOLDER_WATCHER", "false").lower() != "true",
            "time": now_ist()}


@app.get("/api/report-types")
def report_types():
    return {"report_types": [{"key": k, "label": v} for k, v in REPORT_TYPES.items()]}


@app.get("/api/dashboard")
def dashboard():
    ensure_storage()
    reports = load_reports()
    analysis = build_analysis()
    by_type = {r["report_type"] for r in reports}
    analysis["report_counts"] = {"files": len(reports), "types_received": len(by_type - {"unclassified"}), "required": len(REQUIRED_TYPES)}
    analysis["folder_watcher"] = {"enabled": os.getenv("DISABLE_FOLDER_WATCHER", "false").lower() != "true",
                                  "incoming_folder": str(INCOMING_DIR), "last_scan": scan_state["last_scan"], "last_error": scan_state["last_error"]}
    return analysis


@app.post("/api/import")
async def import_reports(files: list[UploadFile] = File(...), report_type: str = Form("auto")):
    ensure_storage()
    results = []
    total = 0
    for file in files:
        raw = await file.read()
        total += len(raw)
        if total > MAX_UPLOAD_BYTES * 3:
            raise HTTPException(413, "Combined upload is too large. Upload reports in smaller batches.")
        try:
            results.append(store_report(file.filename or "report", raw, forced_type=report_type if report_type != "auto" else None))
        except Exception as exc:
            results.append({"file_name": file.filename, "status": "error", "notes": str(exc)})
    return {"uploaded": len(results), "results": results, "dashboard": build_analysis()}


@app.post("/api/import-folder")
def import_folder():
    ensure_storage()
    scan_incoming_folder()
    return {"last_scan": scan_state["last_scan"], "last_error": scan_state["last_error"], "dashboard": build_analysis()}


@app.post("/api/analyze")
def analyze():
    ensure_storage()
    result = build_analysis()
    analysis_id = save_analysis(result)
    result["analysis_id"] = analysis_id
    return result


@app.get("/api/history")
def history():
    ensure_storage()
    with db() as con:
        rows = con.execute("SELECT id,trade_date,created_at,result_json FROM analyses ORDER BY id DESC LIMIT 50").fetchall()
    values = []
    for row in rows:
        obj = json.loads(row["result_json"])
        values.append({"id": row["id"], "trade_date": row["trade_date"], "created_at": row["created_at"], "sentiment": obj.get("sentiment"), "levels": obj.get("levels", {})})
    return {"items": values}


@app.patch("/api/reports/{report_id}")
def reclassify_report(report_id: int, report_type: str = Form(...)):
    if report_type not in REPORT_TYPES:
        raise HTTPException(400, "Unknown report category.")
    with db() as con:
        cur = con.execute("UPDATE reports SET report_type=?,status='parsed',notes='Manually classified by user.' WHERE id=?", (report_type, report_id))
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Report not found.")
    return {"ok": True, "dashboard": build_analysis()}


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: int):
    with db() as con:
        row = con.execute("SELECT stored_path FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Report not found.")
        con.execute("DELETE FROM reports WHERE id=?", (report_id,))
        con.commit()
    try:
        Path(row["stored_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/export")
def export_history():
    ensure_storage()
    with db() as con:
        rows = con.execute("SELECT * FROM analyses ORDER BY id DESC").fetchall()
    data = [{"id": r["id"], "trade_date": r["trade_date"], "created_at": r["created_at"], **json.loads(r["result_json"])} for r in rows]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["analysis_id", "trade_date", "generated_at", "sentiment", "NIFTY_close", "PDC", "PIVOT", "R1", "S1", "R2", "S2", "India_VIX", "PCR", "Max_Pain"])
    for item in data:
        levels = item.get("levels", {}); idx = item.get("index", {}); opt = item.get("options", {})
        writer.writerow([item["id"], item["trade_date"], item["generated_at"], item["sentiment"], idx.get("close"),
                         levels.get("PDC"), levels.get("PIVOT"), levels.get("R1"), levels.get("S1"), levels.get("R2"),
                         levels.get("S2"), item.get("india_vix", {}).get("close"), opt.get("pcr"), opt.get("max_pain")])
    from fastapi.responses import Response
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=nifty_prediction_history.csv"})


@app.get("/api/agents")
def agents():
    return {"agents": [
        {"id": "price", "name": "Price Action", "description": "NIFTY OHLC, classic pivots, CPR and historical price structure."},
        {"id": "options", "name": "Options Intelligence", "description": "NIFTY CE/PE open interest, PCR, Max Pain and strike concentrations."},
        {"id": "institutional", "name": "Institutional Flow", "description": "FII/DII activity, participant positioning and derivatives statistics."},
        {"id": "breadth", "name": "Market Breadth", "description": "Cash-market advances, declines and delivery participation."},
        {"id": "volatility", "name": "Volatility", "description": "India VIX, cash and F&O volatility context."},
        {"id": "ml", "name": "ML Pattern Engine", "description": "Chronological historical pattern model; disabled until enough valid history exists."},
    ]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")), reload=False)
