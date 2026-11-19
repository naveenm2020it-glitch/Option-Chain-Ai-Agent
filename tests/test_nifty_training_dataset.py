import json
from datetime import date, timedelta

import pytest

from scripts.prepare_nifty_training_dataset import (
    _seller_premium_target,
    _weighted_median,
    build_datasets,
)


def test_weighted_median_uses_volume_weights():
    assert _weighted_median([(0.1, 1), (0.2, 8), (0.9, 1)]) == pytest.approx(0.2)


def test_seller_target_matches_exact_expiry_strike_and_option_type():
    strikes = [24900, 24950, 25000, 25050, 25100]
    current = [
        {"expiry": "2026-11-24", "strike": strike, "option_type": "CE", "close": 100,
         "oi": 1000, "volume": 100, "moneyness": (strike - 25000) / 25000}
        for strike in strikes
    ]
    current.append(
        {"expiry": "2026-12-01", "strike": 25000, "option_type": "CE", "close": 200,
         "oi": 1000, "volume": 100, "moneyness": 0.01}
    )
    following = [
        {"expiry": "2026-11-24", "strike": strike, "option_type": "CE", "close": 90}
        for strike in strikes
    ]
    following.append(
        {"expiry": "2026-12-01", "strike": 25000, "option_type": "CE", "close": 100}
    )
    target = _seller_premium_target(current, following, "2026-11-17", "CE")
    assert target["expiry"] == "2026-11-24"
    assert target["matched_contract_count"] == 5
    assert target["premium_return"] == pytest.approx(-0.10)


def _synthetic_checkpoint(count=44):
    start = date(2026, 1, 1)
    features = {}
    for i in range(count):
        day = (start + timedelta(days=i)).isoformat()
        close = 25000 + i * 10
        f = {name: float(i + 1) for name in (
            "spot_close", "spot_open", "spot_high", "spot_low",
            "index_range_pct", "index_close_location", "india_vix_close",
            "days_to_expiry", "ce_oi", "pe_oi", "pcr_oi", "ce_change_oi",
            "pe_change_oi", "pcr_change_oi", "ce_volume", "pe_volume",
            "pcr_volume", "ce_premium_return", "pe_premium_return",
            "ce_writer_build", "pe_writer_build", "ce_writer_build_share",
            "pe_writer_build_share", "ce_max_oi_strike_distance",
            "pe_max_oi_strike_distance", "future_return", "future_oi_change_pct",
            "future_basis_pct", "future_days_to_expiry",
            "participant_oi_fii_option_index_net",
            "participant_oi_client_option_index_net",
            "participant_oi_pro_option_index_net",
            "participant_volume_fii_option_index_net",
            "participant_volume_client_option_index_net",
            "participant_volume_pro_option_index_net",
        )}
        f.update({
            "spot_close": close, "spot_open": close - 5, "spot_high": close + 10,
            "spot_low": close - 10, "index_range_pct": 20 / close,
            "index_close_location": 0.5, "index_data_available": 1.0,
            "future_data_available": 1.0, "participant_oi_data_available": 1.0,
            "participant_volume_data_available": 1.0, "delta_data_available": 0.0,
            "delta_match_coverage": 0.0,
        })
        contracts = []
        for side in ("CE", "PE"):
            for strike in (24900, 24950, 25000, 25050, 25100):
                contracts.append({
                    "expiry": (start + timedelta(days=count + 10)).isoformat(),
                    "strike": strike, "option_type": side, "close": 100 + i,
                    "oi": 1000, "volume": 100, "moneyness": (strike - 25000) / 25000,
                })
        features[day] = {
            "session_date": day, "feature_schema_version": 3,
            "features": f, "contracts": contracts,
        }
    return {"features": features}


def test_build_datasets_uses_22_session_warmup_and_chronological_splits(tmp_path):
    manifest = build_datasets(_synthetic_checkpoint(), tmp_path, expected_sessions=44)
    assert manifest["prepared_sample_count"] == 22
    assert manifest["split_counts"] == {"train": 15, "validation": 3, "holdout": 4}
    assert manifest["training_started"] is False
    assert manifest["seller_target_coverage"] == {"CE": 22, "PE": 22, "available_samples": 22}
    sample = json.loads((tmp_path / "ce_seller" / "train.jsonl").read_text().splitlines()[0])
    assert sample["target_metadata"]["days_to_expiry_on_target"] == 10
    assert (tmp_path / "direction" / "train.jsonl").exists()
    assert (tmp_path / "range" / "holdout.jsonl").exists()
    saved = json.loads((tmp_path / "manifest.json").read_text())
    assert saved["rolling_windows"] == [5, 22]


def test_build_datasets_rejects_bad_ohlc(tmp_path):
    checkpoint = _synthetic_checkpoint()
    first = sorted(checkpoint["features"])[0]
    checkpoint["features"][first]["features"]["spot_high"] = 1.0
    with pytest.raises(ValueError, match="invalid cash-index OHLC"):
        build_datasets(checkpoint, tmp_path, expected_sessions=44)
