import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Optional
import pandas as pd

REPORTS = {
    "nifty50": {"label": "NIFTY 50 Historical Index", "group": "Index", "required": True},
    "india_vix": {"label": "India VIX Historical Index", "group": "Index", "required": True},
    "cm_bhavcopy": {"label": "CM UDiFF Common Bhavcopy Final", "group": "Cash market", "required": True},
    "cm_delivery": {"label": "CM Security-wise Delivery Positions", "group": "Cash market", "required": False},
    "cm_volatility": {"label": "CM Daily Volatility", "group": "Cash market", "required": False},
    "fii_dii": {"label": "FII/FPI and DII Trading Activity", "group": "Institutional", "required": True},
    "fo_bhavcopy": {"label": "F&O UDiFF Common Bhavcopy Final", "group": "Derivatives", "required": True},
    "participant_oi": {"label": "F&O Participant-wise Open Interest", "group": "Derivatives", "required": False},
    "participant_volume": {"label": "F&O Participant-wise Trading Volumes", "group": "Derivatives", "required": False},
    "fii_derivatives": {"label": "F&O FII Derivatives Statistics", "group": "Derivatives", "required": False},
    "fo_settlement": {"label": "F&O Daily Settlement Prices", "group": "Derivatives", "required": False},
    "fo_volatility": {"label": "F&O Daily Volatility", "group": "Derivatives", "required": False},
}


def compact(s):
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def identify_report(filename: str, member_name: str = "") -> str:
    s = compact(f"{filename} {member_name}")
    checks = [
        ("nifty50", ["nifty50", "nifty50index", "niftyhistorical", "nifty50historical"]),
        ("india_vix", ["indiavix", "vixhistorical", "indiavixindex"]),
        ("fii_dii", ["fiidii", "fiifpi", "institutionaltradingactivity", "fpianddii"]),
        ("fo_bhavcopy", ["fo bhavcopy", "bhavcopynsefo", "fo_udiff", "fobhavcopy", "derivativesbhavcopy"]),
        ("cm_bhavcopy", ["cmbhavcopy", "bhavcopycm", "cmudiff", "equitybhavcopy"]),
        ("cm_delivery", ["deliverypositions", "securitywisedelivery", "deliverable", "mto_"]),
        ("participant_oi", ["participantoi", "participantwiseopeninterest", "fao_participant_oi"]),
        ("participant_volume", ["participantvol", "participantwisetradingvolumes", "fao_participant_vol"]),
        ("fii_derivatives", ["fiiderivativesstatistics", "fiistats"]),
        ("fo_settlement", ["settlementprices", "fosett_prce", "settlementprice"]),
        ("fo_volatility", ["fovolt", "fodailyvolatility", "fovolatility"]),
        ("cm_volatility", ["cmvolt", "cmdailyvolatility", "cmvolatility"]),
    ]
    for key, needles in checks:
        if any(compact(n) in s for n in needles):
            return key
    return "unknown"


def _read_bytes(name: str, content: bytes):
    ext = Path(name).suffix.lower()
    if ext in {".xls", ".xlsx"}:
        book = pd.read_excel(io.BytesIO(content), sheet_name=None)
        return [(str(k), v.dropna(how="all")) for k, v in book.items() if isinstance(v, pd.DataFrame) and not v.empty]
    if ext in {".csv", ".txt", ".dat", ".zip"}:
        attempts = []
        for encoding in ("utf-8-sig", "latin1"):
            try:
                df = pd.read_csv(io.BytesIO(content), sep=None, engine="python", encoding=encoding, on_bad_lines="skip")
                if len(df.columns) > 1 or not attempts:
                    return [(name, df.dropna(how="all"))]
            except Exception as exc:
                attempts.append(str(exc))
        try:
            return [(name, pd.read_csv(io.BytesIO(content), encoding="latin1", on_bad_lines="skip").dropna(how="all"))]
        except Exception as exc:
            raise ValueError(f"Unable to read tabular file: {exc}")
    raise ValueError(f"Unsupported report format: {ext or 'unknown'}")


def load_tables(path: str):
    p = Path(path)
    if p.suffix.lower() == ".zip":
        tables = []
        with zipfile.ZipFile(p) as archive:
            for info in archive.infolist():
                if info.is_dir() or Path(info.filename).suffix.lower() not in {".csv", ".dat", ".txt", ".xls", ".xlsx"}:
                    continue
                content = archive.read(info)
                try:
                    parsed = _read_bytes(info.filename, content)
                    tables.extend((member, df) for member, df in parsed)
                except Exception:
                    continue
        return tables
    return _read_bytes(p.name, p.read_bytes())


def dataframe_date(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        c = compact(col)
        if c in {"date", "trddt", "trad dt", "trad dt", "tradingdate", "bizdt", "indexdate", "timestamp", "expirydate"} or "date" in c or c.endswith("dt"):
            parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
            parsed = parsed.dropna()
            if not parsed.empty:
                return parsed.max().date().isoformat()
    return None


def fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def inspect_report(report_type: str, path: str):
    tables = load_tables(path)
    frames = [df for _, df in tables if isinstance(df, pd.DataFrame) and not df.empty]
    if not frames:
        return {"row_count": 0, "columns": [], "trading_date": None, "warnings": ["No readable rows were found in this file."], "table_count": 0}
    total_rows = sum(len(x) for x in frames)
    cols = list(dict.fromkeys(str(c) for df in frames for c in df.columns))[:80]
    date = next((dataframe_date(df) for df in frames if dataframe_date(df)), None)
    warnings = []
    if report_type == "unknown":
        warnings.append("Report type could not be identified from the filename. Rename it using the NSE report name or assign its type in the Data Centre.")
    if date is None:
        warnings.append("Trading date could not be read automatically; choose the date in the Data Centre.")
    return {"row_count": total_rows, "columns": cols, "trading_date": date, "warnings": warnings, "table_count": len(frames)}


def norm_columns(df: pd.DataFrame):
    out = df.copy()
    out.columns = [str(c).strip().replace("\ufeff", "") for c in out.columns]
    return out


def find_column(df: pd.DataFrame, names):
    lookup = {compact(c): c for c in df.columns}
    for name in names:
        if compact(name) in lookup:
            return lookup[compact(name)]
    for col in df.columns:
        token = compact(col)
        for name in names:
            if compact(name) in token and len(compact(name)) >= 4:
                return col
    return None


def numeric(value):
    return pd.to_numeric(value, errors="coerce")
