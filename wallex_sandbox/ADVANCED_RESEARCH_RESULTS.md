# Continued strategy research: not ready

The work expanded substantially, but **no dependable profitable strategy has
been established**. Positive validation and a few winning trades did not
generalize across periods. All new systems remain research scripts; no real
broker or real-money order path was enabled.

## What was implemented

- Captured **260,518 hourly candles** across 12 eligible markets, from May 2024
  through October 2026, using 396 public GET requests. Raw data, hashes and gaps
  are retained; SQLite integrity verified.
- Added regime exits, position retention, completed-candle ATR trailing stops,
  a 72h hard holding cap and exclusion of expensive markets.
- Added actual local Ridge and histogram gradient boosting models at 24h/48h
  horizons, with monthly fitting, purged forward labels, separate calibration,
  saved models, forecasts and explicit abstention.
- Tested weekly retraining with 90 training days and 14 calibration days as a
  targeted response to the monthly model's lag.
- Added matched non-ML controls and a research-readiness assessment that rejects
  validation-only winners, failed cost stress, too few trades and weak later
  performance. This is an assessment, not user permission to run a paper test.

Every strategy comparison kept the 1,000-USDT wallet, maximum 100-USDT all-in
entry and 0.1% planned risk (about 1 USDT initially). Fees stayed at an assumed
0.2% per side; spread/slippage stress remained visible. USDT is a USD proxy.

## Full-period comparisons

All figures below are net USDT in separate 1,000-USDT historical simulations.
They are not new live trades. Recent means July 25–October 23, 2026.

| Candidate | Jul–Dec 2025 validation | Jan–Jun 2026 diagnostic | Recent diagnostic |
|---|---:|---:|---:|
| Original momentum with longer warmup | -6.4680 | -6.7434 | -0.3302 |
| Retained positions plus regime exits | +1.7505 | -2.4380 | +0.6612 |
| 4h ATR trailing primary | +7.0429 | -7.8629 | -2.8385 |
| Monthly gradient boosting, 48h | +3.7965 | 0.0000, no trades | -1.0831 |
| Weekly gradient boosting, 48h | +3.1899 | -1.3682 | +0.5785 |
| Matched liquidity-filtered momentum, 48h | +0.3182 | -8.4133 | +1.9935 |

The weekly model's recent result was **3 wins from 3 trades**, and +0.4878 USDT
under doubled spread/slippage. That is not evidence of a dependable 100% win
rate. The same model had **4 wins from 13 trades** and lost 1.3682 USDT in the
earlier diagnostic; stress worsened that to -1.6441 USDT. It also earned less
than the simple matched control in the recent period.

Those three entries spent only about **8.10–17.36 USDT each**, because the
approximately 1-USDT planned risk and wide daily-ATR stops limited position size.
The wallet budget is 1,000 USDT; it is not all deployed per trade. Increasing size
would change the dollar risk, not establish a better trading edge.

The monthly model had 37 wins from 70 validation trades (52.9%) and stayed
positive under cost stress there. It abstained throughout the first diagnostic,
then had 1 win from 8 recent trades. Its loss avoidance is useful evidence, but
it is not a demonstrated profitable trading edge.

The older +0.8822-USDT exploratory replay used shorter warmup history. The
original momentum row here uses the same longer warmup as its new comparators;
it is the fair baseline for this study. Neither that old green replay nor the
best early ten-trade slice should be presented as reliable future performance.

## Live quote/cost checks

A fresh public scan compared both triangular conversion directions for twelve
eligible assets through USDT and TMN. **None of the 24 routes had a positive
quoted upper-bound margin** after the assumed fees and slippage. Even the least
negative route was about -0.94%, before depth and precision effects. This was a
quote scan, not simulated fills or executed profit. Its assumed TMN fee was
0.35% and USDT-pair fee 0.2%; actual account rates remain unverified.

A separate read-only fee lookup failed before connecting. No actual fee tier
was obtained or used to improve a result. No exchange orders were submitted.

## Verification and limits

**81 checks passed plus 12 subtests.** They cover wallet reconciliation, size and
risk caps, chronological training/calibration boundaries, future-data mutation,
model/ranking prefix invariance, gap fills, trailing-stop timing, retention caps
and rejection of misleading research results. Serialized model hashes and all
completed trade ledgers were independently checked in the generated reports.

The strategies were designed after seeing some later market behavior. These
historical diagnostics are not virgin holdouts or prospective performance.
The weekly iteration explicitly reused already-inspected periods. Monthly and
weekly fitted labels still precede each historical decision; that prevents
training leakage but does not erase research-selection bias.

Historical spreads proxy current snapshots; present-day markets create
survivorship assumptions. OHLC bars cannot reconstruct historical depth,
latency, queues or exact stop fills. Sampled drawdown may miss intrabar extremes.

No sizing increase was run after the rule strategy failed its historical check.
The positive results here were not obtained by increasing the dollar risk.

## Artifacts

- [All rule candidates, validation, later checks and chart](artifacts/long_history_20261023T102235Z/managed_trend/RESULTS.md)
- [Other prequalified rule, checked after primary failure](artifacts/long_history_20261023T102235Z/managed_trend/alternate_check.json)
- [Monthly learned models and matched controls](artifacts/long_history_20261023T102235Z/learned_returns/RESULTS.md)
- [Weekly recency iteration, all outcomes and chart](artifacts/long_history_20261023T102235Z/adaptive_returns/RESULTS.md)
- [Weekly research-readiness rejection](artifacts/long_history_20261023T102235Z/adaptive_returns/readiness.json)
- [Live triangular quote scan](artifacts/triangle_scan_20261023T105408Z/results.json)
- [Raw history manifest](artifacts/long_history_20261023T102235Z/manifest.json)

Adjacent folders contain every trade CSV, prediction CSV, serialized local
model, audit, experiment specification, source/data hash and raw public response.
