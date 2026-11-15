from datetime import date, timedelta
import json

import pytest

from scripts.train_adaptive_intelligence import build_daily_dataset, check_training_gate


def _feature_rows(count=305):
    start = date(2024, 11, 12)
    rows = {}
    for index in range(count):
        day = start + timedelta(days=index)
        rows[day.isoformat()] = {
            "session_date": day.isoformat(),
            "feature_schema_version": 2,
            "features": {
                "spot_close": 25000.0 + index * 500.0,
                "ce_oi": 1000.0 + index,
                "pe_oi": 1200.0 + index * 2,
                "ce_writer_build": 10.0 + index,
                "pe_writer_build": 20.0 + index,
            },
            "contracts": [],
        }
    return rows


def test_training_gate_blocks_before_full_integrity_audit(tmp_path):
    checkpoint = {"features": {}}
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint))
    (tmp_path / "coverage.json").write_text(json.dumps({
        "status": "audit_in_progress",
        "model_training_allowed": False,
        "expected_session_count": 494,
        "verified_session_count": 25,
        "feature_session_count": 25,
        "feature_error_count": 0,
    }))
    with pytest.raises(RuntimeError, match="integrity audit is not complete"):
        check_training_gate(checkpoint_path)


def test_daily_training_labels_use_next_session_only():
    X, y, next_returns, large_moves, breakouts, traps, upper, lower, names = build_daily_dataset(_feature_rows())
    assert len(X) == 303
    assert len(y) == len(next_returns) == len(large_moves) == len(breakouts) == len(traps) == len(upper) == len(lower) == 303
    assert set(y.tolist()) == {2}
    assert next_returns[0] > 0.0025
    assert "spot_return_1d" in names
    assert "spot_close" in names
