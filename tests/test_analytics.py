from nifty_ai.analytics import _pivots


def test_classic_pivot_levels():
    result = _pivots({"high": 100, "low": 90, "close": 96})
    assert result["pivot"] == 95.33333333333333
    assert result["r1"] == 100.66666666666666
    assert result["s1"] == 90.66666666666666
    assert result["r2"] == 105.33333333333333
    assert result["s2"] == 85.33333333333333


def test_missing_prices_do_not_create_levels():
    assert _pivots({"high": None, "low": 90, "close": 95}) == {}
