# Prospective fair-value paper session

Status: **calibrating**. Mode: LIVE_DATA_LOCAL_PAPER.
Snapshot: 2026-10-23T12:12:53.923029+00:00. Deadline: 2026-10-23 16:11:16 UTC.

No exchange orders or money transfers. Wallex fills are local taker simulations using observed depth.

Initial: 1000 USDT. Cash: 1000 USDT.
Last liquidation equity: 1000 USDT; mark time: 2026-10-23 12:12:53 UTC.
Completed round trips: 0. Net winners: 0.
Realized net: 0 USDT. Fees: 0 USDT.
Open paper position: none.

A zero-trade balance is an observation, not proof of trading profitability. Cash is not equity when a position is open.

## Calibration and latest decisions

| Market | Prior samples | Span seconds | Normal local premium | Robust sigma, bps | Latest rejection / eligibility |
|---|---:|---:|---:|---:|---|
| BTCUSDT | 6 | 89.4 | -0.0087% | 1.950 | baseline_warming_up |
| ETHUSDT | 6 | 89.4 | -0.0197% | 2.785 | baseline_warming_up |

Calibration requires at least 90 accepted samples over 900 seconds, using past observations only.
Required potential net return at estimated normal basis: 1.00%, after fees, depth, slippage, exit-spread reserve and basis uncertainty.
That target is conditional on future convergence and is not a demonstrated expected return.

## Costs and data quality

Fee schedule: `{"credential_loaded_by_strategy": false, "fee_asset_assumption": "quote charged on both sides; fee asset was not verified", "lookup_status": "not_verified", "rates": {"BTCUSDT": "0.002", "ETHUSDT": "0.002"}, "source": {"BTCUSDT": "assumed", "ETHUSDT": "assumed"}}`.
Accepted market snapshots: 12. Rejected data snapshots/errors: 2.
Rejection reasons: `{"stale_receipt": 1, "wallex_book_missing": 1}`.

Reference prices use OKX and Bybit spot books quoted in USDT. No USD/USDT conversion is silently assumed.
Receipts must be fresh, snapshot skew bounded, and reference venues in agreement. Wallex has no verified book-event timestamp in this adapter.
A persistent local premium is normalized using the past log-basis median, not labeled a free arbitrage opportunity.
Reference outages block new entries while owned-position exits can re-read Wallex independently.

Files: `status.json`, `trace.sqlite3`, frozen `source_snapshot/`, and `results.json`/`trades.csv` after completion when applicable.
