# Paper trading engine v1 — engineering notes

## Scope
This is an isolated accounting core. It does not fetch market data, generate signals, persist trades, or place orders. No production endpoint or scheduled job is enabled.

## Execution assumptions
- Long option entry executes at the displayed ask; exit executes at displayed bid.
- Adverse slippage is applied to both legs (configurable basis points).
- Quotes must be timezone-aware, positive, non-crossed, and no older than the configured freshness limit.
- Entry and exit must identify the exact same NIFTY contract and lot size.
- Fees are itemized for brokerage, sell-side STT, exchange transaction charge, SEBI turnover fee, buy-side stamp duty and GST.
- Fee rates default to zero deliberately. Before any P&L is treated as meaningful, configure rates from a dated broker/exchange tariff and validate the calculation against that broker's calculator. Do not infer current rates from this code.
- Decimal arithmetic is used for accounting. The engine returns an explicit `is_live_order: false`.

## Not yet implemented
A compliant timestamped market-data adapter, durable trade/quote ledger, signal engine, position sizing and daily risk limits, dashboard/API integration, and 30–60-session forward paper trial. The historic EOD archive does not contain executable intraday bid/ask paths and cannot substitute for them.

## Validation
Run `pytest -q tests/test_paper_trading.py`. Tests cover conservative fills, stale/future/crossed quote rejection, contract identity, and fees reducing P&L.

## Promotion gates
Do not enable live orders. First collect real timestamped option bid/ask quotes, freeze signals before execution, log every rejected/accepted quote, compare paper fills against subsequent market data, and complete a forward trial with positive net expectancy after all costs, controlled drawdown, adequate sample size, and no single-regime dependence.
