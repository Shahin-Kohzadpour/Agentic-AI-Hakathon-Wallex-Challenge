# Indicator and momentum research results

Implemented the researched candidates and obtained a positive **exploratory
historical replay**, with a higher observed net win rate than the original live
0/10. A reliable live improvement has not been established. No real money used.

| Test | Trades | Net wins | Win rate | Net P&L, USDT | Final USDT |
|---|---:|---:|---:|---:|---:|
| Original live one-minute probes | 10 | 0 | 0% | -5.2956 | 994.7044 |
| Hourly Bollinger re-entry: final seven-day holdout | 2 | 0 | 0% | -1.3770 | 998.6230 |
| Two-day momentum: full historical replay | 28 | 12 | 42.9% | +0.8822 | 1000.8822 |
| Two-day momentum: doubled spread/slippage | 28 | 12 | 42.9% | -0.4392 | 999.5608 |
| Weekly momentum reference: full replay | 7 | 2 | 28.6% | +1.3931 | 1001.3931 |
| Weekly momentum reference: doubled spread/slippage | 7 | 2 | 28.6% | +1.2222 | 1001.2222 |

The first ten chronological two-day momentum trades had **6 wins and +2.9149
USDT**; the complete 28-trade series is the relevant result, not just that early
slice. Dates, trade counts and fill assumptions differ from the original live
test, so the table is descriptive rather than a matched-period comparison.

## Implemented changes

- Fixed causal trend pullback, MACD/Bollinger recovery, Supertrend, band re-entry
  and Donchian breakout candidates at 15m and 1h.
- Broadened the eligible spot universe to 12 specified markets, preserving all
  challenge exclusions. Kept the 1,000 USDT budget, 100 USDT maximum all-in
  allocation and approximately 1 USDT planned risk per trade.
- Used verified timestamps, strictly completed aggregates, hourly trend joins
  by completion time, next-bar entries and adverse-first ambiguous stop fills.
- Captured raw public data and measured per-market spreads. Preserved the same
  0.2% fee each side and extra slippage; never lowered fees to obtain a green test.
- Added a slower momentum rule: positive 7/28-day returns, hourly EMA200 filter,
  cost-adjusted momentum ranking, two-daily-ATR stop and scheduled rebalances.
- Added independent wallet reconciliation, risk/cost checks, chronological
  validation, a sealed single holdout and saved source/data hashes.

The first eight-candidate validation failed. The broader twelve-candidate
validation also failed the promotion criteria. The strongest non-empty research
candidate was evaluated with that failure explicitly recorded; its final holdout
also lost. The slower momentum study came afterward and is **exploratory**, not
a replacement untouched holdout. All failures remain saved.

## Practical limits

The two-day result fails doubled execution costs. Both variants' gains came in
August; September and October-to-date lost equity. The weekly reference exceeds
the three-day challenge horizon. A 100-USDT BTC holding reference made +27.2558
USDT over the momentum replay dates, more than either active strategy, with
different exposure and risk. No superiority to passive holding is established.

The new strategies are rule-based and were not enabled in a real broker. The
existing live-paper ranker is still separate. No fresh ten-trade live session is
being represented by these backtests. Current spreads proxy historical spreads;
historical depth and queue effects are unavailable. USDT remains a USD proxy.

**Verification:** 65 checks passed plus 12 subtests, including causal indicators,
timeframe joins, cross-market selection, gap/ambiguous fills, net cost vetoes,
rebalance timing and wallet/risk reconciliation.

- [Momentum report, all variants and equity chart](artifacts/research_data_20261023T095350Z/momentum_exploratory/RESULTS.md)
- [Complete momentum trade and equity data](artifacts/research_data_20261023T095350Z/momentum_exploratory/results.json)
- [Two-day trades CSV](artifacts/research_data_20261023T095350Z/momentum_exploratory/48h_result_trades.csv)
- [First ten two-day trades CSV](artifacts/research_data_20261023T095350Z/momentum_exploratory/48h_first_ten_trades.csv)
- [Failed indicator holdout and all validation results](artifacts/research_data_20261023T095350Z/evaluation/RESULTS.md)
- [Original failed eight-candidate selection](artifacts/research_data_20261023T094937Z/evaluation/selection.json)
- [Public data manifest](artifacts/research_data_20261023T095350Z/manifest.json)

The 90-day momentum replay spans 2026-07-25 09:00 through 2026-10-23 09:00 UTC.
The first eligible entry after feature warmup was August 11. Local historical
capture contains roughly 102 days of hourly observations and 19 days of minute
observations, not 90 days of minute data. The data collection used public GETs;
credentials and exchange order endpoints were not used.
