from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd


FEATURE_SCHEMA_VERSION = 1
REQUIRED_COLUMNS = {
    "TCKRSYMB", "XPRYDT", "STRKPRIC", "OPTNTP", "CLSPRIC",
    "PRVSCLSGPRIC", "OPNINTRST", "CHNGINOPNINTRST",
    "TTLTRADGVOL", "UNDRLYGPRIC",
}


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _read_inner_bhavcopy(outer_bytes: bytes, session_date: date) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(outer_bytes)) as outer:
        manifest = __import__("json").loads(outer.read("manifest.json").decode("utf-8"))
        results = manifest.get("results", {})
        item = results.get("fo_udiff_bhavcopy")
        if not isinstance(item, dict) or not item.get("file"):
            raise ValueError("fo_udiff_bhavcopy is missing from the embedded manifest")
        filename = item["file"]
        if item.get("report_date") != session_date.isoformat():
            raise ValueError("F&O bhavcopy manifest date does not match session date")
        payload = outer.read(filename)

    with zipfile.ZipFile(io.BytesIO(payload)) as inner:
        csv_names = [name for name in inner.namelist() if name.lower().endswith(".csv") and not name.endswith("/")]
        if len(csv_names) != 1:
            raise ValueError(f"Expected one CSV in F&O bhavcopy ZIP; found {len(csv_names)}")
        csv_bytes = inner.read(csv_names[0])
    return pd.read_csv(io.BytesIO(csv_bytes), dtype=str, encoding="utf-8-sig", low_memory=False)


def extract_nifty_features(outer_bytes: bytes, session_date: date) -> dict:
    """Build a NIFTY-only daily feature row and matched-contract snapshots.

    OI + premium combinations are labelled as seller-flow proxies, never as
    proof of the participant who opened the position.
    """
    frame = _read_inner_bhavcopy(outer_bytes, session_date)
    frame.columns = [str(column).replace("\ufeff", "").strip().upper() for column in frame.columns]
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise ValueError("UDiFF F&O bhavcopy missing required columns: " + ", ".join(missing))

    symbol = frame["TCKRSYMB"].astype(str).str.strip().str.upper()
    option_type = frame["OPTNTP"].astype(str).str.strip().str.upper()
    frame = frame.loc[(symbol == "NIFTY") & option_type.isin(["CE", "PE"])].copy()
    if frame.empty:
        raise ValueError("No NIFTY CE/PE contracts found in F&O bhavcopy")

    if "TRADDT" in frame.columns:
        trade_dates = pd.to_datetime(frame["TRADDT"], errors="coerce").dt.date.dropna().unique()
        if len(trade_dates) and set(trade_dates) != {session_date}:
            raise ValueError("F&O bhavcopy trade date does not match archive session date")

    for column in (
        "STRKPRIC", "CLSPRIC", "PRVSCLSGPRIC", "OPNINTRST",
        "CHNGINOPNINTRST", "TTLTRADGVOL", "UNDRLYGPRIC",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["XPRYDT"] = pd.to_datetime(frame["XPRYDT"], errors="coerce").dt.date
    frame = frame.dropna(subset=["XPRYDT", "STRKPRIC", "CLSPRIC", "UNDRLYGPRIC"])
    frame = frame.loc[
        (frame["CLSPRIC"] > 0)
        & (frame["UNDRLYGPRIC"] > 0)
        & (frame["STRKPRIC"] > 0)
    ].copy()
    frame = frame.loc[frame["XPRYDT"] >= session_date]
    if frame.empty:
        raise ValueError("No unexpired NIFTY option contracts remain after validation")

    spot = float(frame["UNDRLYGPRIC"].median())
    if spot <= 0:
        raise ValueError("NIFTY underlying price is invalid")

    expiry = min(frame["XPRYDT"])
    frame = frame.loc[frame["XPRYDT"] == expiry].copy()
    frame["moneyness"] = frame["STRKPRIC"] / spot - 1.0
    near = frame.loc[frame["moneyness"].abs() <= 0.03].copy()
    if len(near) < 4:
        near = frame.reindex(frame["moneyness"].abs().sort_values().index).head(20).copy()
    if near.empty:
        raise ValueError("No usable near-ATM NIFTY options found")

    previous = near["PRVSCLSGPRIC"].where(near["PRVSCLSGPRIC"] > 0)
    near["premium_return"] = (near["CLSPRIC"] / previous - 1.0).replace([float("inf"), -float("inf")], pd.NA)
    near["premium_return"] = near["premium_return"].fillna(0.0)
    near["OPNINTRST"] = near["OPNINTRST"].fillna(0.0).clip(lower=0)
    near["CHNGINOPNINTRST"] = near["CHNGINOPNINTRST"].fillna(0.0)
    near["TTLTRADGVOL"] = near["TTLTRADGVOL"].fillna(0.0).clip(lower=0)

    def side_metrics(side: str) -> dict[str, float]:
        part = near.loc[near["OPTNTP"] == side]
        oi = part["OPNINTRST"]
        change = part["CHNGINOPNINTRST"]
        volume = part["TTLTRADGVOL"]
        premium = part["premium_return"]
        positive_change = change.clip(lower=0)
        negative_change = (-change).clip(lower=0)
        weights = volume.where(volume > 0, oi).clip(lower=0)
        weighted_premium = float((premium * weights).sum() / weights.sum()) if weights.sum() else 0.0
        return {
            "oi": float(oi.sum()),
            "change_oi": float(change.sum()),
            "volume": float(volume.sum()),
            "premium_return": weighted_premium,
            "writer_build": float((positive_change * (-premium).clip(lower=0)).sum()),
            "long_build": float((positive_change * premium.clip(lower=0)).sum()),
            "short_cover": float((negative_change * premium.clip(lower=0)).sum()),
            "long_unwind": float((negative_change * (-premium).clip(lower=0)).sum()),
            "max_oi_strike_distance": float(
                part.loc[oi.idxmax(), "moneyness"] if len(part) and oi.max() > 0 else 0.0
            ),
            "contract_count": float(len(part)),
        }

    ce = side_metrics("CE")
    pe = side_metrics("PE")
    days_to_expiry = (expiry - session_date).days
    features = {
        "spot_close": spot,
        "days_to_expiry": float(days_to_expiry),
        "ce_oi": ce["oi"], "pe_oi": pe["oi"],
        "pcr_oi": _safe_ratio(pe["oi"], ce["oi"]),
        "ce_change_oi": ce["change_oi"], "pe_change_oi": pe["change_oi"],
        "pcr_change_oi": _safe_ratio(pe["change_oi"], abs(ce["change_oi"])),
        "ce_volume": ce["volume"], "pe_volume": pe["volume"],
        "pcr_volume": _safe_ratio(pe["volume"], ce["volume"]),
        "ce_premium_return": ce["premium_return"],
        "pe_premium_return": pe["premium_return"],
        "ce_writer_build": ce["writer_build"],
        "pe_writer_build": pe["writer_build"],
        "ce_long_build": ce["long_build"],
        "pe_long_build": pe["long_build"],
        "ce_short_cover": ce["short_cover"],
        "pe_short_cover": pe["short_cover"],
        "ce_long_unwind": ce["long_unwind"],
        "pe_long_unwind": pe["long_unwind"],
        "ce_writer_build_share": _safe_ratio(ce["writer_build"], abs(ce["change_oi"])),
        "pe_writer_build_share": _safe_ratio(pe["writer_build"], abs(pe["change_oi"])),
        "ce_max_oi_strike_distance": ce["max_oi_strike_distance"],
        "pe_max_oi_strike_distance": pe["max_oi_strike_distance"],
        "near_atm_contract_count": float(len(near)),
        "ce_contract_count": ce["contract_count"],
        "pe_contract_count": pe["contract_count"],
    }

    contracts = []
    for row in near.itertuples(index=False):
        values = row._asdict()
        contracts.append({
            "expiry": values["XPRYDT"].isoformat(),
            "strike": float(values["STRKPRIC"]),
            "option_type": str(values["OPTNTP"]),
            "close": float(values["CLSPRIC"]),
            "previous_close": float(values["PRVSCLSGPRIC"]) if pd.notna(values["PRVSCLSGPRIC"]) else 0.0,
            "oi": float(values["OPNINTRST"]),
            "change_oi": float(values["CHNGINOPNINTRST"]),
            "volume": float(values["TTLTRADGVOL"]),
            "premium_return": float(values["premium_return"]),
            "moneyness": float(values["moneyness"]),
            "days_to_expiry": float(days_to_expiry),
        })

    return {
        "session_date": session_date.isoformat(),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "features": features,
        "contracts": contracts,
    }
