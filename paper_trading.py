"""Deterministic, conservative NIFTY option paper-trade accounting.

No quote fetching, signal generation, or live order placement. Callers must
supply timestamped bid/ask quotes and an explicit, dated fee configuration.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

D = Decimal


class QuoteRejected(ValueError):
    pass


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiry: str
    strike: Decimal
    option_type: Literal["CE", "PE"]
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    lot_size: int
    source: str

    def validate(self, now: datetime, max_age_seconds: int = 15) -> None:
        if self.option_type not in ("CE", "PE"):
            raise QuoteRejected("option_type must be CE or PE")
        if not self.symbol or not self.expiry or not self.source:
            raise QuoteRejected("symbol, expiry and source are required")
        if self.strike <= 0 or self.bid <= 0 or self.ask <= 0:
            raise QuoteRejected("strike, bid and ask must be positive")
        if self.ask < self.bid:
            raise QuoteRejected("crossed quote: ask is below bid")
        if self.lot_size <= 0:
            raise QuoteRejected("lot_size must be positive")
        if self.timestamp.tzinfo is None or now.tzinfo is None:
            raise QuoteRejected("timestamps must be timezone-aware")
        age = (now.astimezone(timezone.utc) - self.timestamp.astimezone(timezone.utc)).total_seconds()
        if age < -2:
            raise QuoteRejected("quote timestamp is in the future")
        if age > max_age_seconds:
            raise QuoteRejected(f"stale quote: age {age:.1f}s exceeds {max_age_seconds}s")


@dataclass(frozen=True)
class FeeConfig:
    """Rates are decimal fractions, sourced from a dated broker/exchange schedule.

    Brokerage is a currency amount per order. GST applies to brokerage,
    exchange transaction charges and SEBI turnover fee. Rates default to zero
    intentionally: a deployment must configure and disclose actual rates.
    """
    brokerage_per_order: Decimal = D("0")
    stt_sell_rate: Decimal = D("0")
    exchange_turnover_rate: Decimal = D("0")
    sebi_turnover_rate: Decimal = D("0")
    stamp_buy_rate: Decimal = D("0")
    gst_rate: Decimal = D("0")

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if value < 0 or (name != "brokerage_per_order" and value >= 1):
                raise ValueError(f"{name} has an invalid non-negative rate/amount")


def _money(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def _fees(turnover: Decimal, is_buy: bool, fee: FeeConfig) -> dict[str, Decimal]:
    brokerage = fee.brokerage_per_order
    exchange = turnover * fee.exchange_turnover_rate
    sebi = turnover * fee.sebi_turnover_rate
    stt = D("0") if is_buy else turnover * fee.stt_sell_rate
    stamp = turnover * fee.stamp_buy_rate if is_buy else D("0")
    gst = (brokerage + exchange + sebi) * fee.gst_rate
    return {k: _money(v) for k, v in {
        "brokerage": brokerage, "exchange_transaction": exchange,
        "sebi": sebi, "stt": stt, "stamp_duty": stamp, "gst": gst,
    }.items()}


def simulate_round_trip(entry: OptionQuote, exit: OptionQuote, lots: int,
                        fees: FeeConfig, *, now: datetime,
                        max_quote_age_seconds: int = 15,
                        slippage_bps: Decimal = D("5")) -> dict:
    """Buy at ask and sell at bid, applying adverse slippage on both legs."""
    fees.validate()
    if lots <= 0:
        raise ValueError("lots must be positive")
    if slippage_bps < 0 or slippage_bps > 1000:
        raise ValueError("slippage_bps must be between 0 and 1000")
    identity = (entry.symbol, entry.expiry, entry.strike, entry.option_type, entry.lot_size)
    other = (exit.symbol, exit.expiry, exit.strike, exit.option_type, exit.lot_size)
    if identity != other:
        raise ValueError("entry and exit quotes must describe the same contract")
    entry.validate(now, max_quote_age_seconds)
    exit.validate(now, max_quote_age_seconds)
    quantity = lots * entry.lot_size
    slip = slippage_bps / D("10000")
    buy_fill = entry.ask * (D("1") + slip)
    sell_fill = exit.bid * (D("1") - slip)
    buy_turnover, sell_turnover = buy_fill * quantity, sell_fill * quantity
    buy_fees, sell_fees = _fees(buy_turnover, True, fees), _fees(sell_turnover, False, fees)
    gross = (sell_fill - buy_fill) * quantity
    charges = sum(buy_fees.values(), D("0")) + sum(sell_fees.values(), D("0"))
    net = gross - charges
    return {
        "contract": {"symbol": entry.symbol, "expiry": entry.expiry,
                     "strike": str(entry.strike), "option_type": entry.option_type,
                     "lot_size": entry.lot_size},
        "quantity": quantity, "lots": lots,
        "entry": {"quote_time": entry.timestamp.isoformat(), "source": entry.source,
                  "bid": str(entry.bid), "ask": str(entry.ask),
                  "simulated_fill": str(_money(buy_fill)), "turnover": str(_money(buy_turnover)),
                  "fees": {k: str(v) for k, v in buy_fees.items()}},
        "exit": {"quote_time": exit.timestamp.isoformat(), "source": exit.source,
                 "bid": str(exit.bid), "ask": str(exit.ask),
                 "simulated_fill": str(_money(sell_fill)), "turnover": str(_money(sell_turnover)),
                 "fees": {k: str(v) for k, v in sell_fees.items()}},
        "gross_pnl": str(_money(gross)), "total_charges": str(_money(charges)),
        "net_pnl": str(_money(net)), "slippage_bps_per_side": str(slippage_bps),
        "execution_model": "buy_at_ask_sell_at_bid_plus_adverse_slippage",
        "is_live_order": False,
    }
