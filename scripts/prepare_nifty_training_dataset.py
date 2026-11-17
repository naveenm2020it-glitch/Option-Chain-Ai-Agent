from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import date
from pathlib import Path


BASE_FEATURES = (
    "spot_close", "spot_open", "spot_high", "spot_low",
    "index_range_pct", "index_close_location", "india_vix_close",
    "days_to_expiry",
    "ce_oi", "pe_oi", "pcr_oi", "ce_change_oi", "pe_change_oi",
    "pcr_change_oi", "ce_volume", "pe_volume", "pcr_volume",
    "ce_premium_return", "pe_premium_return",
    "ce_writer_build", "pe_writer_build",
    "ce_writer_build_share", "pe_writer_build_share",
    "ce_max_oi_strike_distance", "pe_max_oi_strike_distance",
    "future_return", "future_oi_change_pct", "future_basis_pct",
    "future_days_to_expiry",
    "participant_oi_fii_option_index_net",
    "participant_oi_client_option_index_net",
    "participant_oi_pro_option_index_net",
    "participant_volume_fii_option_index_net",
    "participant_volume_client_option_index_net",
    "participant_volume_pro_option_index_net",
)
MASK_FEATURES = (
    "index_data_available", "future_data_available",
    "participant_oi_data_available", "participant_volume_data_available",
    "delta_data_available", "delta_match_coverage",
)
DIRECTION_NEUTRAL_BAND = 0.0015
SELLER_EDGE_BAND = 0.01
MIN_OPTION_PREMIUM = 5.0
MAX_MONEYNESS = 0.03
MIN_MATCHED_CONTRACTS = 5
ROLLING_WINDOW_SHORT = 5
ROLLING_WINDOW_LONG = 22


def _finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _weighted_median(values: list[tuple[float, float]]) -> float:
    if not values:
        raise ValueError("weighted median requires at least one value")
    cleaned = [(float(value), max(0.0, float(weight))) for value, weight in values]
    cleaned.sort(key=lambda item: item[0])
    total_weight = sum(weight for _, weight in cleaned)
    if total_weight <= 0:
        return float(statistics.median(value for value, _ in cleaned))
    threshold = total_weight / 2.0
    cumulative = 0.0
    for value, weight in cleaned:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return cleaned[-1][0]


def _contract_key(contract: dict) -> tuple[str, float, str]:
    return (
        str(contract.get("expiry", "")),
        round(float(contract.get("strike", 0.0)), 2),
        str(contract.get("option_type", "")).upper(),
    )


def _seller_premium_target(
    current_contracts: list[dict],
    next_contracts: list[dict],
    next_date: str,
    option_type: str,
) -> dict | None:
    next_map = {}
    for contract in next_contracts:
        key = _contract_key(contract)
        if key in next_map:
            raise ValueError(f"duplicate next-session contract key: {key}")
        next_map[key] = contract

    candidates_by_expiry: dict[str, list[tuple[float, float]]] = {}
    for contract in current_contracts:
        if str(contract.get("option_type", "")).upper() != option_type:
            continue
        expiry = str(contract.get("expiry", ""))
        if not expiry or expiry < next_date:
            continue
        moneyness = _finite_number(contract.get("moneyness"), "contract.moneyness")
        oi = _finite_number(contract.get("oi"), "contract.oi")
        volume = _finite_number(contract.get("volume"), "contract.volume")
        premium = _finite_number(contract.get("close"), "contract.close")
        if abs(moneyness) > MAX_MONEYNESS or oi <= 0 or volume <= 0 or premium < MIN_OPTION_PREMIUM:
            continue
        key = _contract_key(contract)
        next_contract = next_map.get(key)
        if not next_contract:
            continue
        next_premium = _finite_number(next_contract.get("close"), "next_contract.close")
        if premium <= 0 or next_premium <= 0:
            continue
        change = next_premium / premium - 1.0
        candidates_by_expiry.setdefault(expiry, []).append((change, volume))

    for expiry in sorted(candidates_by_expiry):
        values = candidates_by_expiry[expiry]
        if len(values) < MIN_MATCHED_CONTRACTS:
            continue
        return {
            "premium_return": _weighted_median(values),
            "expiry": expiry,
            "matched_contract_count": len(values),
            "volume_weight_total": sum(weight for _, weight in values),
        }
    return None


def _feature_vector(history: list[dict]) -> dict[str, float]:
    if len(history) != ROLLING_WINDOW_LONG:
        raise ValueError("feature history must contain exactly 22 sessions")
    current = history[-1]
    result: dict[str, float] = {}
    for name in BASE_FEATURES:
        values = [_finite_number(row.get(name), name) for row in history]
        result[f"{name}__current"] = values[-1]
        short_values = values[-ROLLING_WINDOW_SHORT:]
        result[f"{name}__mean_5"] = statistics.fmean(short_values)
        result[f"{name}__std_5"] = statistics.pstdev(short_values)
        result[f"{name}__mean_22"] = statistics.fmean(values)
        result[f"{name}__std_22"] = statistics.pstdev(values)
    for name in MASK_FEATURES:
        result[f"{name}__current"] = _finite_number(current.get(name), name)
    previous_close = _finite_number(history[-2].get("spot_close"), "previous.spot_close")
    current_close = _finite_number(current.get("spot_close"), "current.spot_close")
    result["spot_return_1d__current"] = current_close / previous_close - 1.0
    return result


def _direction_label(value: float) -> str:
    if value > DIRECTION_NEUTRAL_BAND:
        return "UP"
    if value < -DIRECTION_NEUTRAL_BAND:
        return "DOWN"
    return "NEUTRAL"


def _seller_label(premium_return: float) -> str:
    if premium_return < -SELLER_EDGE_BAND:
        return "SELLER_FAVORABLE"
    if premium_return > SELLER_EDGE_BAND:
        return "BUYER_FAVORABLE"
    return "NEUTRAL"


def build_datasets(checkpoint: dict, output_dir: Path, expected_sessions: int = 494) -> dict:
    rows = checkpoint.get("features")
    if not isinstance(rows, dict) or len(rows) != expected_sessions:
        raise ValueError(f"expected {expected_sessions} feature sessions")
    dates = sorted(rows)
    if dates != sorted(set(dates)):
        raise ValueError("duplicate session dates")
    for day in dates:
        date.fromisoformat(day)
        row = rows[day]
        if row.get("session_date") != day or row.get("feature_schema_version") != 3:
            raise ValueError(f"session/schema mismatch on {day}")
        f = row.get("features", {})
        for flag in ("index_data_available", "future_data_available",
                     "participant_oi_data_available", "participant_volume_data_available"):
            if _finite_number(f.get(flag), f"{day}.{flag}") < 1.0:
                raise ValueError(f"required context unavailable on {day}: {flag}")
        close = _finite_number(f.get("spot_close"), f"{day}.spot_close")
        open_value = _finite_number(f.get("spot_open"), f"{day}.spot_open")
        high = _finite_number(f.get("spot_high"), f"{day}.spot_high")
        low = _finite_number(f.get("spot_low"), f"{day}.spot_low")
        if close <= 0 or low <= 0 or high < max(open_value, low, close) or low > min(open_value, high, close):
            raise ValueError(f"invalid cash-index OHLC on {day}")
        if abs(_finite_number(f.get("index_range_pct"), f"{day}.index_range_pct") - (high - low) / close) > 1e-8:
            raise ValueError(f"stale index_range_pct on {day}")
        expected_location = (close - low) / (high - low) if high > low else 0.5
        if abs(_finite_number(f.get("index_close_location"), f"{day}.index_close_location") - expected_location) > 1e-8:
            raise ValueError(f"stale index_close_location on {day}")
        for name in BASE_FEATURES + MASK_FEATURES:
            _finite_number(f.get(name), f"{day}.{name}")
        contracts = row.get("contracts", [])
        keys = [_contract_key(contract) for contract in contracts]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate contract snapshot key on {day}")
        for contract in contracts:
            if str(contract.get("expiry", "")) < day:
                raise ValueError(f"expired contract in session snapshot on {day}")

    prepared = []
    seller_coverage = {"CE": 0, "PE": 0}
    for index in range(ROLLING_WINDOW_LONG - 1, len(dates) - 1):
        day, next_day = dates[index], dates[index + 1]
        history = [rows[d]["features"] for d in dates[index - ROLLING_WINDOW_LONG + 1:index + 1]]
        vector = _feature_vector(history)
        current = rows[day]
        following = rows[next_day]
        current_close = _finite_number(current["features"]["spot_close"], "spot_close")
        next_features = following["features"]
        next_close = _finite_number(next_features["spot_close"], "next.spot_close")
        next_high = _finite_number(next_features["spot_high"], "next.spot_high")
        next_low = _finite_number(next_features["spot_low"], "next.spot_low")
        direction_return = next_close / current_close - 1.0
        targets = {
            "direction_return": direction_return,
            "direction_class": _direction_label(direction_return),
            "next_high_move_pct": next_high / current_close - 1.0,
            "next_low_move_pct": next_low / current_close - 1.0,
            "next_range_pct": (next_high - next_low) / current_close,
        }
        metadata = {}
        for side in ("CE", "PE"):
            target = _seller_premium_target(
                current.get("contracts", []),
                following.get("contracts", []),
                next_day,
                side,
            )
            if target:
                seller_coverage[side] += 1
                targets[f"{side.lower()}_premium_return"] = target["premium_return"]
                targets[f"{side.lower()}_seller_edge"] = -target["premium_return"]
                targets[f"{side.lower()}_seller_class"] = _seller_label(target["premium_return"])
                metadata[f"{side.lower()}_target_expiry"] = target["expiry"]
                metadata[f"{side.lower()}_matched_contract_count"] = target["matched_contract_count"]
            else:
                targets[f"{side.lower()}_premium_return"] = None
                targets[f"{side.lower()}_seller_edge"] = None
                targets[f"{side.lower()}_seller_class"] = None
                metadata[f"{side.lower()}_target_expiry"] = None
                metadata[f"{side.lower()}_matched_contract_count"] = 0
        prepared.append({
            "session_date": day,
            "target_date": next_day,
            "features": vector,
            "targets": targets,
            "metadata": metadata,
        })

    if not prepared:
        raise ValueError("no rows remain after the 22-session warmup")
    total = len(prepared)
    train_end = int(total * 0.70)
    validation_end = int(total * 0.85)
    split_rows = {
        "train": prepared[:train_end],
        "validation": prepared[train_end:validation_end],
        "holdout": prepared[validation_end:],
    }
    if not all(split_rows.values()):
        raise ValueError("chronological split produced an empty partition")
    for left, right in (("train", "validation"), ("validation", "holdout")):
        if split_rows[left][-1]["target_date"] >= split_rows[right][0]["target_date"]:
            raise ValueError("target-date overlap across chronological splits")

    model_specs = {
        "direction": ("direction_return", "direction_class"),
        "range": ("next_range_pct", None),
        "ce_seller": ("ce_premium_return", "ce_seller_class"),
        "pe_seller": ("pe_premium_return", "pe_seller_class"),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_names = sorted(prepared[0]["features"])
    split_summary = {}
    for model_name, (target_name, class_name) in model_specs.items():
        split_summary[model_name] = {}
        for split_name, split in split_rows.items():
            selected = []
            for row in split:
                value = row["targets"].get(target_name)
                if value is None:
                    continue
                selected.append({
                    "session_date": row["session_date"],
                    "target_date": row["target_date"],
                    "features": row["features"],
                    "target": value,
                    "target_class": row["targets"].get(class_name) if class_name else None,
                    "targets": row["targets"] if model_name == "range" else None,
                    "metadata": row["metadata"],
                })
            model_dir = output_dir / model_name
            model_dir.mkdir(parents=True, exist_ok=True)
            with (model_dir / f"{split_name}.jsonl").open("w", encoding="utf-8") as handle:
                for item in selected:
                    handle.write(json.dumps(item, separators=(",", ":"), allow_nan=False) + "\n")
            split_summary[model_name][split_name] = {
                "rows": len(selected),
                "first_target_date": selected[0]["target_date"] if selected else None,
                "last_target_date": selected[-1]["target_date"] if selected else None,
            }

    direction_counts = Counter(row["targets"]["direction_class"] for row in prepared)
    ce_counts = Counter(row["targets"]["ce_seller_class"] for row in prepared if row["targets"]["ce_seller_class"])
    pe_counts = Counter(row["targets"]["pe_seller_class"] for row in prepared if row["targets"]["pe_seller_class"])
    manifest = {
        "schema_version": 1,
        "source_window": [dates[0], dates[-1]],
        "source_session_count": len(dates),
        "prepared_sample_count": total,
        "warmup_sessions": ROLLING_WINDOW_LONG,
        "feature_count": len(feature_names),
        "base_feature_count": len(BASE_FEATURES),
        "rolling_windows": [ROLLING_WINDOW_SHORT, ROLLING_WINDOW_LONG],
        "split_policy": "chronological by target_date; 70% train / 15% validation / 15% holdout; no shuffle",
        "split_counts": {name: len(value) for name, value in split_rows.items()},
        "model_splits": split_summary,
        "target_definitions": {
            "direction": f"next-session NIFTY close return; neutral band +/-{DIRECTION_NEUTRAL_BAND:.4f}",
            "range": "next-session high/low/range relative to current session close",
            "ce_seller": "volume-weighted median next-session premium return for exact matched front-expiry CE contracts; seller edge is its negative",
            "pe_seller": "volume-weighted median next-session premium return for exact matched front-expiry PE contracts; seller edge is its negative",
            "seller_label": f"SELLER_FAVORABLE below -{SELLER_EDGE_BAND:.3f}; BUYER_FAVORABLE above +{SELLER_EDGE_BAND:.3f}; otherwise NEUTRAL",
            "contract_matching": "same expiry + strike + option type only; current moneyness within 3%; current OI/volume positive; premium >= 5 points; at least 5 matches",
        },
        "seller_target_coverage": {
            "CE": seller_coverage["CE"],
            "PE": seller_coverage["PE"],
            "available_samples": total,
        },
        "class_counts": {
            "direction": dict(direction_counts),
            "ce_seller": dict(ce_counts),
            "pe_seller": dict(pe_counts),
        },
        "leakage_policy": "Features use current and prior 21 sessions only. Next-session values are used only to construct targets. Contract targets match the exact expiry, strike and option type.",
        "training_started": False,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare four chronological NIFTY model datasets without training.")
    parser.add_argument("--checkpoint", default="data/historical_audit/checkpoint.json")
    parser.add_argument("--output-dir", default="data/prepared_models")
    parser.add_argument("--expected-sessions", type=int, default=494)
    args = parser.parse_args()
    checkpoint = json.loads(Path(args.checkpoint).read_text(encoding="utf-8"))
    result = build_datasets(checkpoint, Path(args.output_dir), args.expected_sessions)
    print(json.dumps(result, indent=2))
