from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd


FEATURE_SCHEMA_VERSION = 1
REQUIRED_COLUMNS = {
    "TCKRSYMB", "FININSTRMTP", "XPRYDT", "STRKPRIC", "OPTNTP", "CLSPRIC",
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
    nifty_rows = frame.loc[symbol == "NIFTY"].copy()
    if nifty_rows.empty:
        raise ValueError("No NIFTY instruments found in F&O bhavcopy")

    if "TRADDT" in nifty_rows.columns:
        trade_dates = pd.to_datetime(nifty_rows["TRADDT"], errors="coerce").dt.date.dropna().unique()
        if len(trade_dates) and set(trade_dates) != {session_date}:
            raise ValueError("F&O bhavcopy trade date does not match archive session date")

    for column in (
        "STRKPRIC", "CLSPRIC", "PRVSCLSGPRIC", "OPNINTRST",
        "CHNGINOPNINTRST", "TTLTRADGVOL", "UNDRLYGPRIC",
    ):
        nifty_rows[column] = pd.to_numeric(nifty_rows[column], errors="coerce")

    nifty_rows["XPRYDT"] = pd.to_datetime(nifty_rows["XPRYDT"], errors="coerce").dt.date
    option_frame = nifty_rows.loc[
        option_type.reindex(nifty_rows.index).isin(["CE", "PE"])
        & (nifty_rows["FININSTRMTP"].astype(str).str.upper() == "IDO")
    ].copy()
    option_frame = option_frame.dropna(subset=["XPRYDT", "STRKPRIC", "CLSPRIC", "UNDRLYGPRIC"])
    option_frame = option_frame.loc[
        (option_frame["CLSPRIC"] > 0)
        & (option_frame["UNDRLYGPRIC"] > 0)
        & (option_frame["STRKPRIC"] > 0)
        & (option_frame["XPRYDT"] >= session_date)
    ].copy()
    if option_frame.empty:
        raise ValueError("No unexpired NIFTY CE/PE index-option contracts remain after validation")

    spot = float(option_frame["UNDRLYGPRIC"].median())
    if spot <= 0:
        raise ValueError("NIFTY underlying price is invalid")

    option_frame["moneyness"] = option_frame["STRKPRIC"] / spot - 1.0
    expiry = min(option_frame["XPRYDT"])
    near_expiry = option_frame.loc[option_frame["XPRYDT"] == expiry].copy()
    near = near_expiry.loc[near_expiry["moneyness"].abs() <= 0.03].copy()
    if len(near) < 4:
        near = near_expiry.reindex(near_expiry["moneyness"].abs().sort_values().index).head(20).copy()
    if near.empty:
        raise ValueError("No usable near-ATM NIFTY options found")

    all_near = option_frame.loc[
        (option_frame["moneyness"].abs() <= 0.03)
        & ((option_frame["XPRYDT"] - session_date).map(lambda value: value.days) <= 45)
    ].copy()
    if len(all_near) < len(near):
        all_near = near.copy()

    future_frame = nifty_rows.loc[
        nifty_rows["FININSTRMTP"].astype(str).str.upper() == "IDF"
    ].copy()
    future_frame = future_frame.dropna(subset=["XPRYDT", "CLSPRIC"])
    future_frame = future_frame.loc[
        (future_frame["CLSPRIC"] > 0) & (future_frame["XPRYDT"] >= session_date)
    ].copy()

    previous = near["PRVSCLSGPRIC"].where(near["PRVSCLSGPRIC"] > 0)
    near["premium_return"] = (near["CLSPRIC"] / previous - 1.0).replace([float("inf"), -float("inf")], pd.NA)
    near["premium_return"] = near["premium_return"].fillna(0.0)
    near["OPNINTRST"] = near["OPNINTRST"].fillna(0.0).clip(lower=0)
    near["CHNGINOPNINTRST"] = near["CHNGINOPNINTRST"].fillna(0.0)
    near["TTLTRADGVOL"] = near["TTLTRADGVOL"].fillna(0.0).clip(lower=0)

    def side_metrics(data: pd.DataFrame, side: str) -> dict[str, float]:
        part = data.loc[data["OPTNTP"] == side]
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

    ce = side_metrics(near, "CE")
    pe = side_metrics(near, "PE")
    all_ce = side_metrics(all_near, "CE")
    all_pe = side_metrics(all_near, "PE")
    days_to_expiry = (expiry - session_date).days
    if not future_frame.empty:
        future_expiry = min(future_frame["XPRYDT"])
        front_future = future_frame.loc[future_frame["XPRYDT"] == future_expiry]
        future_close = float(front_future["CLSPRIC"].median())
        future_previous = float(front_future["PRVSCLSGPRIC"].median())
        future_oi = float(front_future["OPNINTRST"].fillna(0).sum())
        future_change_oi = float(front_future["CHNGINOPNINTRST"].fillna(0).sum())
        future_volume = float(front_future["TTLTRADGVOL"].fillna(0).sum())
        future_return = _safe_ratio(future_close - future_previous, future_previous)
        future_basis = _safe_ratio(future_close - spot, spot)
        future_days_to_expiry = float((future_expiry - session_date).days)
    else:
        future_close = future_previous = future_oi = future_change_oi = future_volume = 0.0
        future_return = future_basis = future_days_to_expiry = 0.0
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
        "all_expiry_ce_oi": all_ce["oi"],
        "all_expiry_pe_oi": all_pe["oi"],
        "all_expiry_pcr_oi": _safe_ratio(all_pe["oi"], all_ce["oi"]),
        "all_expiry_ce_change_oi": all_ce["change_oi"],
        "all_expiry_pe_change_oi": all_pe["change_oi"],
        "all_expiry_ce_volume": all_ce["volume"],
        "all_expiry_pe_volume": all_pe["volume"],
        "all_expiry_ce_writer_build": all_ce["writer_build"],
        "all_expiry_pe_writer_build": all_pe["writer_build"],
        "all_expiry_ce_premium_return": all_ce["premium_return"],
        "all_expiry_pe_premium_return": all_pe["premium_return"],
        "future_data_available": 1.0 if not future_frame.empty else 0.0,
        "future_close": future_close,
        "future_previous_close": future_previous,
        "future_return": future_return,
        "future_oi": future_oi,
        "future_change_oi": future_change_oi,
        "future_volume": future_volume,
        "future_oi_change_pct": _safe_ratio(future_change_oi, abs(future_oi - future_change_oi)),
        "future_basis_pct": future_basis,
        "future_days_to_expiry": future_days_to_expiry,
        "all_expiry_contract_count": float(len(all_near)),
    }

    contracts = []
    for row in all_near.itertuples(index=False):
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
