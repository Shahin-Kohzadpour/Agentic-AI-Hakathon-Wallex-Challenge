# Cost-aware paper strategy revision

The original 10-trade run lost **5.2956 USDT** on a 1,000 USDT wallet.
The largest cause was paying costs without requiring a forecast that could cover
them. Completing ten trades took precedence over entry quality. This was useful
for exercising execution, but a poor basis for a performance experiment.

## Recorded loss attribution

| Component | USDT |
|---|---:|
| Assumed fees, 0.2% per side | -3.9801 |
| Spread, depth impact and additional slippage | -1.1715 |
| Midpoint market movement | -0.1439 |
| Total net | -5.2956 |

All ten original selected forecasts were negative after estimated costs. The
old one-minute forecast also had too little time to earn back those costs.
These figures describe the original recorded paper session; its artifacts have
not been rewritten.

## Implemented defaults

- A trade count is a ceiling. A session can finish with zero entries.
- Forecast and holding horizon both use 15 minutes.
- Train, calibration and validation periods run in chronological order with
  purged boundaries and non-overlapping scored return intervals.
- The model must beat a zero-return forecast in validation, with at least three
  cost-qualified historical entries and positive aggregate net return.
- An empirical overprediction adjustment reduces the current forecast. The
  adjusted return must cover estimated round-trip costs plus 10 basis points.
- Spreads over 15 basis points are rejected. Fresh proposed-size depth must pass
  the cost gate again before a paper entry can debit cash.
- Fees stay at the original assumed 0.2% per side, with 1 basis point additional
  slippage each side. No fee discount or maker-fill assumption improves results.
- Stops and targets use net liquidation proceeds. Sizing adds a 0.1%-of-equity
  planned-risk cap; maximum all-in spend remains 100 USDT on the initial wallet.
- A 15-minute cooldown prevents immediate re-entry. Entries need enough session
  time to observe the forecast horizon.

These filters improve entry discipline. They do not establish a profitable
model. The empirical forecast adjustment has no guaranteed statistical coverage;
validation uses a small candle sample and today's assumed costs, not historical
order-book fills. Stop prices do not guarantee achievable execution.

## Checks and live audit

**51 checks passed, plus 12 subtests**, covering the existing sandbox and tick
checks, public endpoint restrictions, paper ledger, chronological boundaries,
cost vetoes, abstention, fee-aware exit valuation and zero-trade completion.

The new minimum cost gate rejects **10/10 original decisions**. This is a
retrospective veto of saved decisions, not a fresh return or proof that the full
revised strategy would have earned money during that period.

A separate read-only audit ran on **2026-10-23, 07:15:12–07:16:36 UTC**
(10:45:12–10:46:36 Tehran), using 17 public GET requests:

- Three prospective live scans, zero qualifying signals.
- BTC gross 15-minute forecasts were about +0.0030% to +0.0034%, versus estimated
  costs of 0.519% to 0.559%, before the extra entry margin.
- BTC also failed the out-of-sample comparison with a zero-return forecast.
- ETH and SOL exceeded the spread limit on all three scans. The audit records
  six candidate rejections as `data_errors`; these were spread rejections, not
  failed HTTP requests.
- No paper fills or real orders were executed. The audit wallet stayed at 1,000
  USDT because it was a signal audit; that balance is not strategy performance.

[Raw live audit](artifacts/strategy_audit_20261023T071512Z/audit.json)
contains the current forecasts, cost hurdles, rejection reasons, validation
statistics and baseline comparison. The neighboring SQLite trace retains raw
public responses and full model decisions. Original run: [results](LIVE_PAPER_RESULTS.md).

To audit again or run the revised simulated trading loop, see [README](README.md).
