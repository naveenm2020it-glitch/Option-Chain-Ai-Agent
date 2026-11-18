from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import pytest
from paper_trading import OptionQuote, FeeConfig, QuoteRejected, simulate_round_trip

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)

def q(bid, ask, ts=NOW):
    return OptionQuote("NIFTY", "2026-11-26", D("26000"), "CE",
                       D(str(bid)), D(str(ask)), ts, 65, "test-feed")

def test_conservative_fill_and_net_pnl():
    r = simulate_round_trip(q(99, 101), q(109, 111), 1, FeeConfig(),
                            now=NOW, slippage_bps=D("0"))
    assert r["entry"]["simulated_fill"] == "101.00"
    assert r["exit"]["simulated_fill"] == "109.00"
    assert r["net_pnl"] == "520.00"
    assert r["is_live_order"] is False

def test_stale_quote_rejected():
    with pytest.raises(QuoteRejected, match="stale"):
        simulate_round_trip(q(99, 101, NOW-timedelta(seconds=16)), q(109,111),
                            1, FeeConfig(), now=NOW)

def test_crossed_market_rejected():
    with pytest.raises(QuoteRejected, match="crossed"):
        simulate_round_trip(q(102,101), q(109,111), 1, FeeConfig(), now=NOW)

def test_contract_mismatch_rejected():
    other = OptionQuote("NIFTY", "2026-11-26", D("26100"), "CE",
                        D("109"), D("111"), NOW, 65, "test-feed")
    with pytest.raises(ValueError, match="same contract"):
        simulate_round_trip(q(99,101), other, 1, FeeConfig(), now=NOW)

def test_fees_reduce_pnl():
    a = simulate_round_trip(q(99,101), q(109,111), 1, FeeConfig(),
                            now=NOW, slippage_bps=D("0"))
    b = simulate_round_trip(q(99,101), q(109,111), 1,
                            FeeConfig(brokerage_per_order=D("20")),
                            now=NOW, slippage_bps=D("0"))
    assert D(b["net_pnl"]) < D(a["net_pnl"])

def test_future_quote_rejected():
    with pytest.raises(QuoteRejected, match="future"):
        simulate_round_trip(q(99,101,NOW+timedelta(seconds=10)), q(109,111),
                            1, FeeConfig(), now=NOW)
