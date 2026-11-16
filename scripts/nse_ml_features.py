from __future__ import annotations

import io
import re
import zipfile
from datetime import date

import pandas as pd


FEATURE_SCHEMA_VERSION = 3
REQUIRED_COLUMNS = {
    "TCKRSYMB", "FININSTRMTP", "XPRYDT", "STRKPRIC", "OPTNTP", "CLSPRIC",
    "PRVSCLSGPRIC", "OPNINTRST", "CHNGINOPNINTRST",
    "TTLTRADGVOL", "UNDRLYGPRIC",
}


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _outer_csv_frame(outer_bytes: bytes, report_key: str) -> pd.DataFrame | None:
    with zipfile.ZipFile(io.BytesIO(outer_bytes)) as outer:
        manifest = __import__("json").loads(outer.read("manifest.json").decode("utf-8"))
        item = manifest.get("results", {}).get(report_key)
        if not isinstance(item, dict) or not item.get("file"):
            return None
        payload = outer.read(item["file"])
    # NSE has used both compact and expanded CSV headers. Some report exports
    # also prepend a title line; locate the real header before parsing.
    decoded = payload.decode("utf-8-sig")
    lines = decoded.splitlines()
    header_index = 0
    if report_key in {"fo_participant_oi", "fo_participant_volume"}:
        for index, line in enumerate(lines[:10]):
            normalized = _normalised_name(line)
            if "clienttype" in normalized and "futureindexlong" in normalized:
                header_index = index
                break
    elif report_key == "nifty_index_close_all":
        for index, line in enumerate(lines[:10]):
            normalized = _normalised_name(line)
            if "indexname" in normalized and ("closing" in normalized or "close" in normalized):
                header_index = index
                break
    return pd.read_csv(io.StringIO("\n".join(lines[header_index:])), dtype=str, low_memory=False)


def _normalised_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _participant_features(frame: pd.DataFrame | None, prefix: str) -> dict[str, float]:
    if frame is None or frame.empty:
        return {f"{prefix}_data_available": 0.0}
    columns = {_normalised_name(column): column for column in frame.columns}
    participant_column = next(
        (columns[key] for key in ("clienttype", "participanttype", "participant") if key in columns),
        frame.columns[0],
    )
    result = {f"{prefix}_data_available": 0.0}
    categories = {
        "fii": ("fii", "fpi", "fii/fpi", "foreign institutional investor", "foreign portfolio investor"),
        "dii": ("dii",),
        "client": ("client", "clients"),
        "pro": ("pro", "proprietary"),
        "total": ("total",),
    }
    groups = (
        ("future_index_long", "futureindexlong"),
        ("future_index_short", "futureindexshort"),
        ("option_index_call_long", "optionindexcalllong"),
        ("option_index_call_short", "optionindexcallshort"),
        ("option_index_put_long", "optionindexputlong"),
        ("option_index_put_short", "optionindexputshort"),
        ("future_stock_long", "futurestocklong"),
        ("future_stock_short", "futurestockshort"),
        ("option_stock_call_long", "optionstockcalllong"),
        ("option_stock_call_short", "optionstockcallshort"),
        ("option_stock_put_long", "optionstockputlong"),
        ("option_stock_put_short", "optionstockputshort"),
        ("total_long", "totallongcontracts"),
        ("total_short", "totalshortcontracts"),
    )
    participant_values = frame[participant_column].astype(str).map(_normalised_name)
    categories_found = set()
    for category, aliases in categories.items():
        normalized_aliases = {_normalised_name(alias) for alias in aliases}
        matches = frame.loc[participant_values.isin(normalized_aliases)]
        if matches.empty:
            continue
        categories_found.add(category)
        row = matches.iloc[-1]
        values = {}
        for feature_name, normalized_column in groups:
            source_column = columns.get(normalized_column)
            if source_column is None:
                continue
            numeric = pd.to_numeric(str(row[source_column]).replace(",", ""), errors="coerce")
            values[feature_name] = float(numeric) if pd.notna(numeric) else 0.0
            result[f"{prefix}_{category}_{feature_name}"] = values[feature_name]
        if "future_index_long" in values and "future_index_short" in values:
            result[f"{prefix}_{category}_future_index_net"] = values["future_index_long"] - values["future_index_short"]
        if "option_index_call_long" in values and "option_index_call_short" in values:
            result[f"{prefix}_{category}_option_index_call_net"] = values["option_index_call_long"] - values["option_index_call_short"]
        if "option_index_put_long" in values and "option_index_put_short" in values:
            result[f"{prefix}_{category}_option_index_put_net"] = values["option_index_put_long"] - values["option_index_put_short"]
        if all(key in values for key in (
            "option_index_call_long", "option_index_call_short",
            "option_index_put_long", "option_index_put_short",
        )):
            result[f"{prefix}_{category}_option_index_net"] = (
                values["option_index_call_long"] + values["option_index_put_long"]
                - values["option_index_call_short"] - values["option_index_put_short"]
            )
    required_index_columns = {
        "futureindexlong", "futureindexshort",
        "optionindexcalllong", "optionindexcallshort",
        "optionindexputlong", "optionindexputshort",
    }
    # Participant files are contextual NSE-wide index-derivatives totals.
    # Mark them usable only when all four participant categories and the core
    # index futures/options long/short columns are present.
    if {"fii", "dii", "client", "pro"}.issubset(categories_found) and required_index_columns.issubset(columns):
        result[f"{prefix}_data_available"] = 1.0
    return result


def _contract_delta_map(frame: pd.DataFrame | None, session_date: date) -> dict[tuple[str, float, str], float]:
    if frame is None or frame.empty:
        return {}
    columns = {_normalised_name(column): column for column in frame.columns}
    required = ("symbol", "expiryday", "strikeprice", "optiontype", "deltafactor")
    if any(key not in columns for key in required):
        return {}
    data = frame.copy()
    symbol = data[columns["symbol"]].astype(str).str.strip().str.upper()
    option_type = data[columns["optiontype"]].astype(str).str.strip().str.upper()
    data = data.loc[(symbol == "NIFTY") & option_type.isin(("CE", "PE"))].copy()
    data["_expiry"] = pd.to_datetime(data[columns["expiryday"]], errors="coerce").dt.date
    data["_strike"] = pd.to_numeric(data[columns["strikeprice"]].astype(str).str.replace(",", ""), errors="coerce")
    data["_delta"] = pd.to_numeric(data[columns["deltafactor"]].astype(str).str.replace(",", ""), errors="coerce")
    data = data.dropna(subset=["_expiry", "_strike", "_delta"])
    data = data.loc[data["_expiry"] >= session_date]
    result = {}
    for _, row in data.iterrows():
        delta = float(row["_delta"])
        if 1.0 < abs(delta) <= 100.0:
            delta /= 100.0
        if abs(delta) > 1.0:
            continue
        option = str(row[columns["optiontype"]]).strip().upper()
        delta = abs(delta) if option == "CE" else -abs(delta)
        key = (row["_expiry"].isoformat(), round(float(row["_strike"]), 2), option)
        result[key] = delta
    return result


def _index_features(frame: pd.DataFrame | None) -> dict[str, float]:
    defaults = {
        "index_data_available": 0.0, "spot_open": 0.0, "spot_high": 0.0,
        "spot_low": 0.0, "india_vix_close": 0.0,
    }
    if frame is None or frame.empty:
        return defaults
    columns = {_normalised_name(column): column for column in frame.columns}
    name_column = next((columns[key] for key in ("indexname", "index") if key in columns), None)
    if name_column is None:
        return defaults
    names = frame[name_column].astype(str).map(_normalised_name)
    nifty = frame.loc[names.isin(("nifty50", "nifty"))]
    vix = frame.loc[names.isin(("indiavix", "indiavolatilityindex"))]
    def value(row, aliases):
        column = next((columns[key] for key in aliases if key in columns), None)
        if column is None or row is None:
            return 0.0
        number = pd.to_numeric(str(row[column]).replace(",", ""), errors="coerce")
        return float(number) if pd.notna(number) else 0.0
    if nifty.empty:
        return defaults
    row = nifty.iloc[-1]
    vix_row = vix.iloc[-1] if not vix.empty else None
    # NSE archive variants use either "Open Index Value"/"Closing Index Value"
    # or the compact "Open"/"Closing" names.
    close = value(row, ("closingindexvalue", "closeindexvalue", "closing", "close"))
    high = value(row, ("highindexvalue", "high"))
    low = value(row, ("lowindexvalue", "low"))
    open_value = value(row, ("openindexvalue", "open"))
    vix_close = value(vix_row, ("closingindexvalue", "closeindexvalue", "closing", "close"))
    return {
        "index_data_available": 1.0 if close > 0 and open_value > 0 and high >= close and low > 0 and high > low else 0.0,
        "index_close": close,
        "spot_open": open_value,
        "spot_high": high,
        "spot_low": low,
        "india_vix_close": vix_close,
    }


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
    required_numeric = ["STRKPRIC", "CLSPRIC", "OPNINTRST", "CHNGINOPNINTRST",
                        "TTLTRADGVOL", "UNDRLYGPRIC"]
    if option_frame[required_numeric].isna().any().any():
        bad = [column for column in required_numeric if option_frame[column].isna().any()]
        raise ValueError("NIFTY option rows contain missing required numeric fields: " + ", ".join(bad))
    if (option_frame[["STRKPRIC", "CLSPRIC", "OPNINTRST", "TTLTRADGVOL", "UNDRLYGPRIC"]] < 0).any().any():
        raise ValueError("NIFTY option rows contain negative strike, price, OI, volume or underlying values")
    if (option_frame["STRKPRIC"] <= 0).any() or (option_frame["CLSPRIC"] <= 0).any():
        raise ValueError("NIFTY option rows contain non-positive strike or close")
    duplicate_keys = option_frame.duplicated(["XPRYDT", "STRKPRIC", "OPTNTP"], keep=False)
    if duplicate_keys.any():
        raise ValueError("NIFTY option bhavcopy contains duplicate expiry/strike/option-type contracts")
    option_frame = option_frame.loc[
        (option_frame["CLSPRIC"] > 0)
        & (option_frame["UNDRLYGPRIC"] > 0)
        & (option_frame["STRKPRIC"] > 0)
        & (option_frame["XPRYDT"] >= session_date)
    ].copy()
    if option_frame.empty:
        raise ValueError("No unexpired NIFTY CE/PE index-option contracts remain after validation")

    index_info = _index_features(_outer_csv_frame(outer_bytes, "nifty_index_close_all"))
    delta_map = _contract_delta_map(_outer_csv_frame(outer_bytes, "fo_contract_delta"), session_date)
    participant_oi = _participant_features(_outer_csv_frame(outer_bytes, "fo_participant_oi"), "participant_oi")
    participant_volume = _participant_features(_outer_csv_frame(outer_bytes, "fo_participant_volume"), "participant_volume")
    spot = float(index_info.get("index_close") or option_frame["UNDRLYGPRIC"].median())
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

    def prepare_option_data(data: pd.DataFrame) -> pd.DataFrame:
        data = data.copy()
        previous = data["PRVSCLSGPRIC"].where(data["PRVSCLSGPRIC"] > 0)
        data["premium_return"] = (data["CLSPRIC"] / previous - 1.0).replace([float("inf"), -float("inf")], pd.NA)
        data["premium_return"] = data["premium_return"].fillna(0.0)
        data["OPNINTRST"] = data["OPNINTRST"].fillna(0.0).clip(lower=0)
        data["CHNGINOPNINTRST"] = data["CHNGINOPNINTRST"].fillna(0.0)
        data["TTLTRADGVOL"] = data["TTLTRADGVOL"].fillna(0.0).clip(lower=0)
        return data

    near = prepare_option_data(near)
    all_near = prepare_option_data(all_near)

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
    all_near["delta_factor"] = [
        delta_map.get((row["XPRYDT"].isoformat(), round(float(row["STRKPRIC"]), 2), str(row["OPTNTP"]).upper()), 0.0)
        for _, row in all_near.iterrows()
    ]
    ce_delta_equiv_oi = float((all_near.loc[all_near["OPTNTP"] == "CE", "OPNINTRST"] * all_near.loc[all_near["OPTNTP"] == "CE", "delta_factor"]).sum())
    pe_delta_equiv_oi = float((all_near.loc[all_near["OPTNTP"] == "PE", "OPNINTRST"] * all_near.loc[all_near["OPTNTP"] == "PE", "delta_factor"]).sum())
    delta_matched_contract_count = sum(1 for value in all_near["delta_factor"] if value != 0.0)
    days_to_expiry = (expiry - session_date).days
    if not future_frame.empty:
        future_expiry = min(future_frame["XPRYDT"])
        front_future = future_frame.loc[future_frame["XPRYDT"] == future_expiry]
        future_close = float(front_future["CLSPRIC"].median())
        future_previous = float(front_future["PRVSCLSGPRIC"].fillna(0).median())
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
        "spot_open": float(index_info.get("spot_open", 0.0) or spot),
        "spot_high": float(index_info.get("spot_high", 0.0) or spot),
        "spot_low": float(index_info.get("spot_low", 0.0) or spot),
        "index_data_available": float(index_info.get("index_data_available", 0.0)),
        "india_vix_close": float(index_info.get("india_vix_close", 0.0)),
        "index_range_pct": _safe_ratio(
            float(index_info.get("spot_high", 0.0)) - float(index_info.get("spot_low", 0.0)),
            spot,
        ),
        "index_close_location": _safe_ratio(
            spot - float(index_info.get("spot_low", 0.0)),
            float(index_info.get("spot_high", 0.0)) - float(index_info.get("spot_low", 0.0)),
        ),
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
        "delta_data_available": 1.0 if delta_map else 0.0,
        "delta_matched_contract_count": float(delta_matched_contract_count),
        "delta_match_coverage": _safe_ratio(delta_matched_contract_count, len(all_near)),
        "ce_delta_equiv_oi": ce_delta_equiv_oi,
        "pe_delta_equiv_oi": pe_delta_equiv_oi,
        "net_option_delta_equiv_oi": ce_delta_equiv_oi + pe_delta_equiv_oi,
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
            "days_to_expiry": float((values["XPRYDT"] - session_date).days),
            "delta_factor": float(values["delta_factor"]),
        })

    features.update(participant_oi)
    features.update(participant_volume)
    return {
        "session_date": session_date.isoformat(),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "features": features,
        "contracts": contracts,
    }
