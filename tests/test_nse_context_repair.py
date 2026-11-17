import pytest

from scripts.nse_context_repair import _refresh_index_derived_features


def test_context_repair_recalculates_index_range_and_close_location():
    features = {
        "index_data_available": 1.0,
        "index_close": None,
        "spot_close": 100.0,
        "spot_open": 98.0,
        "spot_high": 104.0,
        "spot_low": 96.0,
        "index_range_pct": 0.0,
        "index_close_location": 0.0,
    }
    assert _refresh_index_derived_features(features) is True
    assert features["index_close"] == 100.0
    assert features["index_range_pct"] == pytest.approx(0.08)
    assert features["index_close_location"] == pytest.approx(0.5)


def test_context_repair_rejects_invalid_index_ohlc():
    features = {
        "index_data_available": 1.0,
        "spot_close": 100.0,
        "spot_open": 98.0,
        "spot_high": 95.0,
        "spot_low": 96.0,
    }
    assert _refresh_index_derived_features(features) is False


def test_context_repair_leaves_unavailable_index_fields_untouched():
    features = {
        "index_data_available": 0.0,
        "spot_close": 100.0,
        "spot_open": 100.0,
        "spot_high": 100.0,
        "spot_low": 100.0,
    }
    assert _refresh_index_derived_features(features) is False
    assert "index_range_pct" not in features


