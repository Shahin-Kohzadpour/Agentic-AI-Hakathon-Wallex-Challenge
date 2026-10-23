# Strategy research for the Wallex paper bot

Researched 2026-10-23. This is a source review and a proposed experiment design,
not a backtest or a claim of improved measured trading performance. The current
runner was not changed by this research. No exchange credentials or orders used.

## Findings and evidence quality

| System / research | What was verified | Useful part for this project | What was not established |
|---|---|---|---|
| [Nof1](https://www.nof1.ai/) | Its current official site describes financial-model research and the difficulty current LLMs have in markets. It links Alpha Arena. | Treat model skill as something to measure, rather than assuming a powerful model will trade profitably. | Current Arena performance and the original full strategy were not verified. The linked Arena technical post returned 403. |
| [Hummingbot MACD + Bollinger controller](https://github.com/hummingbot/hummingbot/blob/master/controllers/directional_trading/macd_bb_v1.py) | Executable rules: Bollinger length 100, standard deviation 2; MACD 21/42/9. Long when below lower Bollinger band, histogram positive, MACD line negative. Default interval 3m. | A small, explicit recovery/mean-reversion signal compatible with our existing project family. | Source code does not establish profitable returns or a transferable win rate. |
| [Hummingbot Supertrend controller](https://github.com/hummingbot/hummingbot/blob/master/controllers/directional_trading/supertrend_v1.py) | ATR-based Supertrend, default length 20 and multiplier 4, directional signal only near the trend line. | An independently testable trend baseline and volatility-aware signal. | No verified live profit or Wallex execution support from this file. |
| [kojott/LLM-trader-test](https://github.com/kojott/LLM-trader-test) | A Nof1-inspired project documents multi-timeframe reasoning, indicator inputs, paper accounting and structured decisions. It also offers real-execution options. | Borrow the separation of higher-timeframe trend, local structure, entries, and position state. | Not official Nof1 code or an independently verified profitable strategy. Do not run its real-execution path. |
| [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | Freqtrade strategy project; README requires 5m and recommends a broad stablecoin pair universe. Its license is GPL-3.0. | Reference for strategy organization and market selection, rather than another whole runtime to integrate now. | Advertised/community win rates were not verified. Its configuration does not map directly to our narrow eligible universe or risk budget. |
| [Freqtrade](https://docs.freqtrade.io/en/latest/strategy-101/) | Documents historical backtests, live-data dry runs, and pitfalls in strategy rankings. [Lookahead analysis](https://docs.freqtrade.io/en/2026.5/lookahead-analysis/) checks whether future data contaminates signals. | Use its testing discipline: causal indicators, realistic costs, untouched holdout and prospective evaluation. | A framework is not a profitable strategy and does not itself add Wallex support. |

There is actual primary research behind momentum as a strategy family:
[Liu and Tsyvinski, Risks and Returns of Cryptocurrency](https://www.nber.org/papers/w24877)
reports a historical time-series momentum effect in cryptocurrency returns.
[AQR's trend-following study](https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing)
reports historical results over many traditional markets. These are historical
studies at different horizons and venues; neither validates our proposed 15m
entries, current Wallex fees, or a three-day challenge return.

## First candidate: trend-filtered pullback

This is **our proposed test specification**, not a copied Nof1 strategy or a
published winning configuration. Parameters should be frozen before evaluation.

1. Spot longs only; retain all challenge exclusions, cash and depth limits.
2. Use completed 1h candles: price above EMA200, EMA50 above EMA200, EMA50 rising
   relative to three completed hourly candles earlier. Otherwise remain in cash.
3. On completed 15m candles, require a prior candle pullback below EMA20 and a
   current close recovering above EMA20. RSI14 must cross upward through 50.
   Current volume must exceed the mean of the previous 20 completed candles.
4. Initial stop distance: 1.5 times ATR14 on the 15m chart. Position sizing must
   account for both fees and execution friction, using the current small risk cap.
5. Evaluate a target at twice that stop distance, but require at least a 1.5:1
   **net** reward/risk ratio after all estimated costs. Skip insufficient setups.
   Target distance is not a model forecast or proof the target will be reached.
6. Exit on target, stop, an hourly regime breakdown, or a four-hour maximum hold.
   These durations are proposed starting values, not optimized values.
7. Apply the existing spread, fresh-depth, budget and cooldown controls. Do not
   close after one minute merely to complete a requested number of trades.

This separates trend, entry timing, volatility and costs. It is preferable to
adding many overlapping indicators to the current ridge model without evidence.

## Second candidate: filtered MACD/Bollinger recovery

Reproduce the Hummingbot MACD/Bollinger long condition above, then separately test
our adaptations: 15m execution and the completed 1h trend filter. Keep original
and modified results separate. A falling coin being below a lower band is not
enough to enter; the histogram condition asks for a momentum recovery. Add the
same cost, position-risk and maximum-holding controls as the first candidate.

Compare a basic Supertrend long-only rule as a simple baseline. Do not test dozens
of variants and present the best historical one as an independently proven result.

## How to determine whether this actually improves anything

- Retrieve sufficient Wallex history for indicator warmup and multiple market
  conditions; aim for 60–90 days where data availability permits. Record gaps and
  do not substitute Binance history while labeling results Wallex results.
- Split chronologically into development, validation and an untouched final
  holdout. Any rule revision consumes the validation period; retain the final
  holdout until the experiment specification is frozen.
- Signals use only candles closed by decision time. Earliest simulated entry is
  the next available candle/open or later observed book. Higher-timeframe merges
  must use completion time. If stop and target touch inside the same candle,
  assume the adverse stop first unless finer data resolves the path.
- Preserve the original 0.2% assumed fee per side. Include spread, slippage and
  stressed costs; historical candle fills need explicit assumptions because we
  do not possess historical order books. Never presume maker fills simply because
  an order price touched a candle.
- Compare net account P&L, average net trade, profit factor, drawdown, exposure,
  number of trades and win rate against cash and buy-and-hold with the same costs.
  Open inventory must be valued at executable liquidation value.
- After selecting and freezing a candidate, run a new 1,000-USDT prospective
  paper account. Report all eligible trades and abstentions; don't force ten
  trades within an arbitrary short deadline.

Win rate alone can be misleading. For example, 80% wins averaging +0.2% and 20%
losses averaging -2% produce -0.24% expected return per trade before costs. A
useful improvement needs positive net expectancy, not just one green trade.

## Current conclusion

Keep Condor and the GET-only paper adapter. First evaluate the trend-filtered
pullback, with MACD/Bollinger recovery and Supertrend as alternatives. None has
yet earned a profitable label on our data. Our original 0/10 is the result of
one short cost-dominated experiment, not an estimate that every future strategy
will have a zero probability of winning.
