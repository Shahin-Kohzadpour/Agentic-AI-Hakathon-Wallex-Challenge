# Prospective fair-value paper research

This implements the execution-first research candidate: synchronized external
spot reference prices, Wallex's normal local premium, and executable local depth.
It is a hypothesis under prospective testing, not a proven profitable strategy.
Every wallet/order/fill is local. There is no exchange order submission method.

## Data and signal

- BTC/USDT and ETH/USDT only, checked against Wallex's public spot metadata and
  the supplied exclusion manifest.
- Public Wallex depth plus OKX and Bybit **spot**, all quoted in USDT. External
  prices are reference data; no funds or orders go to another venue.
- Require two independent references, receipts within 3 seconds, quote age no
  more than 8 seconds, and reference disagreement no more than 15 bps. Exchange
  timestamps are checked where present. Wallex freshness here is receipt/cache
  freshness, not a verified exchange-event timestamp.
- Estimate `log(Wallex midpoint / reference midpoint)` from a trailing two-hour
  window. Calibration requires 90 accepted samples over at least 15 minutes;
  with network time it may take longer. Data gaps reset calibration.
- Evaluate each new observation against **prior** samples before adding it.
  Use the median local premium and a median-absolute-deviation uncertainty
  adjustment. This avoids calling an ordinary local discount an opportunity.
- Model a return to the estimated normal premium, reserve an exit spread, and
  subtract both fees, actual entry VWAP impact and extra slippage. This is a
  **potential return at a conditional target**, not a forecast of convergence.
- Require potential net return >=max(0.2%, planned stop x minimum reward/risk).
  With defaults that means **1% potential net return**. It may produce no trades.
- A qualified gap must persist for 30 seconds and at least three observations.
  Re-fetch all three books and repeat the cost/freshness tests before entry.

The first live preflight captured aligned BTC/ETH quotes, rejecting two
asynchronous snapshots. It was observation-only, not a profitability test.

## Fill and risk model

The 1,000-USDT wallet treats USDT as a USD proxy. One long spot position can
spend at most 100 USDT / 10% equity, with a 0.1%-of-equity planned risk cap and a
1% net-liquidation stop. Gaps, polling delay and slippage can exceed planned loss.

Paper buys walk current asks; paper sells walk bids. Both add 1 bp adverse
slippage. New entries require enough nearby visible depth and impact <=10 bps.
Only 5% of depth within that impact band may be consumed. No passive fill is
inferred from a candle touch. Maker fees are not assumed cheaper.

Condor's actual risk callback checks entries and owned exits. The outer loop and
paper wallet are custom; this is not a native Hummingbot Wallex connector.

Exit when the local basis recovers, net stop triggers, two hours elapse, account
drawdown reaches 2%, or the session winds down. Basis recovery can still lose
money if the global reference price falls. Reference outages do not disable
owned exits: Wallex is re-read independently after stale/slow reference results.

The default session runs four hours, takes at most ten completed round trips,
and avoids new entries when too little time remains to observe a full holding
window. Ten is a ceiling, not a quota. A completed zero-trade session is valid
data collection and is not evidence of profitable trading.

## Fee verification

`wallex_fee_probe` is a separate, fixed-URL **GET-only** helper for the Wallex
account fee endpoint. Only it reads the user-supplied local secret file. It
never follows redirects, logs credentials, exports account volume, or contacts
external reference venues. The strategy accepts only its sanitized JSON report.

The actual lookup on 2026-10-23 returned **HTTP 401**, so rates remain unverified.
The simulation explicitly retains assumed **0.2% taker fees per side**. Fee
charging in quote currency is also an assumption; even a verified rate lookup
does not prove the fee asset. A stale fee report falls back to labeled assumptions.

No key value appears in a command, report, raw quote trace, model, or commit.
The strategy removes inherited API-key variables, disables implicit dotenv and
netrc authentication, and contains only reviewed public market-data GET paths.

## Run and inspect

```bash
.venv/bin/python -m wallex_sandbox.wallex_fee_probe --key-file '<existing-local-key-file>' --output wallex_sandbox/artifacts/fair_fee_schedule.json
.venv/bin/python -u -m wallex_sandbox.fair_value_paper --minutes 240 --fee-file wallex_sandbox/artifacts/fair_fee_schedule.json
.venv/bin/python -m wallex_sandbox.report_fair_value <run-directory>
.venv/bin/python -m pytest -q wallex_sandbox
```

Use `--observe-only --minutes 2` for adapter verification without any paper fills.
`--resume <run-directory>` requires unchanged configuration, source and fee
schedule. Paper wallet and calibration history are durable; entry persistence
resets on restart. Source snapshots and hashes are retained with every new run.

The SQLite journal contains paired normalized books, raw public responses,
timestamps, decisions, rejection reasons, simulated fills and atomic wallet
checkpoints. `status.json` updates while running. `results.json` and, when there
are trades, `trades.csv` are written at completion. The report command is safe
while the writer is running. Cash and liquidation equity are reported separately.

Send SIGTERM to the recorded PID for a paper wind-down. The runner attempts a
fresh local exit; after five minutes without an executable quote it reports an
incomplete open paper position instead of fabricating a fill.

## Verification and limits

The complete sandbox suite passed **100 checks plus 21 subtests**. New checks
cover native risk approvals, refreshing quotes before debiting cash, stops
during reference outages, persistence resets, baseline causality, wrong quote
currencies, stale/asynchronous/disagreeing feeds, shallow depth, fees, cooldown,
trade ceilings, atomic wallet recovery and fee-report secret filtering.

There is no statistical proof yet that this local basis converges quickly or
often enough to earn costs. Short calibration and correlated observations can
misestimate a normal premium. Snapshot liquidity can disappear before a real
order arrives. Actual fees remain unverified. These are material research limits.

Primary adapter references: [Bybit spot order book](https://bybit-exchange.github.io/docs/v5/market/orderbook),
[OKX market data](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-order-book),
[Wallex API](https://api-docs.wallex.ir/).
