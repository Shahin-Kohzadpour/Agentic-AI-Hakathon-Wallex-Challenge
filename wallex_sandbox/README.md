# Condor + Wallex offline paper sandbox

Chosen base: [hummingbot/condor](https://github.com/hummingbot/condor), MIT licensed.
Downloaded revision: `8e4860fa3d7efd4b55c2b74dd7f1f439e5a4f32c`.
Local branch: `wallex/paper-sandbox`.

The offline runner exercises **Condor's real RiskEngine and permission callback** using
a local fake exchange. It includes the supplied 345-pair exclusion manifest.
Balances start at a synthetic 5,000,000 TMN. Prices, model decisions, order fills,
and the 0.3% fee per side are deliberately artificial.

No Wallex credential is loaded. The offline runner has no HTTP exchange client.
IP connections are blocked in its check process. Its model responses are scripted,
so no external AI-provider account or paid inference is used. Condor's main app,
Telegram integration, Docker services, and real trading bots are never started.

## Run

From the Condor checkout, use the isolated Python 3.13 environment:

```bash
.venv/bin/python -m wallex_sandbox.run
.venv/bin/python -m wallex_sandbox.checks --upstream
```

Results and transcripts are written into timestamped folders under
`wallex_sandbox/artifacts/`. The JSON summary, verbose log and upstream JUnit XML
record what passed and the Condor revision. Each demonstration gets a fresh fake
wallet so rerunning cannot accidentally reuse an earlier demo's balance.

## Checks

- A synthetic buy/sell round trip, fees, partial sells and insufficient inventory.
- Oversized and cumulatively oversized orders, executor count, leverage, ownership.
- Banned markets, foreign connectors, stale/missing quotes and malformed amounts.
- Condor dry-run refusal of mutations and arbitrary code, while reads remain available.
- Drawdown blocking, emergency owned-position exits and an untrusted book.
- Duplicate intent IDs, changed arguments, and timeout-after-acceptance recovery.
- Durable fake cash, positions and fees across restart; blocked IP connections.
- Actual Condor tick orchestration with a scripted model: gather, streamed tool call,
  permission refusal, saved experiment, model teardown, and persisted model failures.
- Selected upstream risk, drawdown and tick lifecycle checks.

## Environment

The machine's system Python is 3.14; Condor declares Python `>=3.12,<3.14`.
Python 3.13 was installed separately. The environment uses a small set of headless
dependencies for these checks. The full app's speech, plotting, desktop/browser
and other optional workflows were not installed or launched.

For a fresh headless environment:

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r wallex_sandbox/requirements.lock.txt
```

Use the explicit `.venv/bin/python` commands above. `uv run` would synchronize the
whole Condor application instead of this deliberately smaller environment.

## Cost-aware live public-data paper session

This mode uses public Wallex GET endpoints. All balances and fills remain local
simulations; there is no exchange order-submission method or credential loader.
The default strategy now abstains unless its conservative forecast covers fees,
spread, slippage and another 10 basis points of estimated net return.

```bash
.venv/bin/python -m wallex_sandbox.audit_strategy --scans 3 --interval 30
.venv/bin/python -m wallex_sandbox.live_paper --trades 10 --hold-seconds 900 --max-minutes 40
.venv/bin/python -m pytest -q wallex_sandbox/test_live_paper.py wallex_sandbox/test_strategy.py
```

`--trades 10` is a maximum, never a quota. A complete session can have zero trades.
The first command only audits live signals and never executes even paper entries.
Its unchanged 1,000 USDT cash is not a measured strategy return. Both modes record
public requests, models, decisions and rejection reasons in a timestamped SQLite
trace under `wallex_sandbox/artifacts/`.

The model uses about 24 hours of closed one-minute candles, with 15-minute forward
labels aligned with the holding period. A fixed ridge model trains on the first
60%, estimates overprediction on the next 20%, and validates on the final 20%.
Labels crossing boundaries are purged. Calibration and validation observations
have non-overlapping return intervals. A market must beat a zero-return forecast
in validation and have at least three cost-qualified validation entries with
positive aggregate net return before it can qualify. The final model refits on
available past labels; recorded validation results are never recomputed using
that final fit. There is no parameter search or LLM call.

The empirical 80th percentile overprediction adjustment is a conservative screen,
not a guaranteed confidence bound. This is a small candle-based historical screen
at today's cost hurdle, not a historical order-book backtest. The historical net
sum is not an account return or evidence that future orders will profit.

BTC/USDT, ETH/USDT and SOL/USDT must pass live spot metadata and the supplied
345-pair exclusion list. Spreads above 15 basis points are rejected. Entries walk
current asks, exits walk bids, with 1 basis point extra adverse slippage per side
and the unchanged assumed 0.2% fee per side. Account fees remain unverified. Entry
costs reserve an exit spread at the configured limit and are rechecked against
fresh executable depth and the proposed quantity before any balance changes.

The simulated wallet starts at 1,000 USDT (USD proxy). One position can spend up to
10% of equity, capped at 100 USDT, with an additional sizing cap of 0.1% equity
risk divided by the 1% planned net stop. Stops and targets use liquidation value
after both fees and exit slippage. The target is 0.8% net; the time limit is 15
minutes. Stops are triggers, not guaranteed fills. A 15-minute cooldown prevents
immediate re-entry. The runner avoids entering when its observation window cannot
cover the forecast horizon. Condor's actual risk and owned-exit callbacks still
check every paper fill; the outer orchestrator is custom, not native TickEngine.

The original ten forced one-minute trades and their reports are preserved. To
reproduce an execution-only probe explicitly:

```bash
.venv/bin/python -m wallex_sandbox.live_paper --exploratory --trades 10 --hold-seconds 60
```

That flag bypasses the entry evidence gate and is unsuitable for performance
validation. It is not an exact reproduction of all original risk defaults.
Live paper runs write `status.json`, `trace.sqlite3`, `trades.csv` (when there are
trades), `results.json` and `RESULTS.md`. Render completed trading sessions with:

```bash
.venv/bin/python -m wallex_sandbox.plot_live_paper <run-directory>
```

## Indicator and lower-turnover research

See [INDICATOR_RESULTS.md](INDICATOR_RESULTS.md) for the measured outcomes,
including failed validation/holdout tests and the positive but fragile exploratory
momentum replay. This research does not replace the live-paper ranker by default.

```bash
.venv/bin/python -m wallex_sandbox.collect_research_data --days 90 --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT LTCUSDT BCHUSDT TRXUSDT
.venv/bin/python -m wallex_sandbox.backtest_candidates <new-capture-directory> --phase development
.venv/bin/python -m wallex_sandbox.backtest_candidates <new-capture-directory> --phase validation
.venv/bin/python -m wallex_sandbox.backtest_candidates <new-capture-directory> --phase holdout
.venv/bin/python -m wallex_sandbox.momentum_research <new-capture-directory>
.venv/bin/python -m wallex_sandbox.report_momentum <capture-directory>
```

The ordinary holdout path refuses to evaluate an unqualified strategy. The saved
study used `evaluate_research_candidate` explicitly after qualification failed;
its outcome is labeled exploratory and does not enable paper/live orders.
Experiment specifications and selection files are frozen before evaluation;
rerunning to overwrite an opened holdout or momentum experiment is refused.
Reports may be rendered again without rerunning the strategy.

The observed API response to `resolution=15` contained minute-spaced timestamps.
The collector validates timestamps and aggregates complete minute bars rather
than trusting that query label. Requested 90-day minute history is truncated by
the provider; the saved manifest records actual coverage and gaps. Hourly history
was fetched in chunks. No synthetic bars replace missing observations.

MACD/Bollinger and Supertrend candidates are independent reimplementations
inspired by Hummingbot's published controllers, not bit-identical pandas-ta
outputs. The original controller's default timeframe was changed explicitly for
these experiments. All source and configuration hashes are retained in results.

## Continued research: regime management and learned returns

[ADVANCED_RESEARCH_RESULTS.md](ADVANCED_RESEARCH_RESULTS.md) records the latest
status. It includes losing diagnostics and the tiny recent winning sample; no
new strategy is represented as profitable or connected to a real broker.

```bash
.venv/bin/python -m wallex_sandbox.collect_long_history --metadata <existing-capture-directory> --start 2024-05-01
.venv/bin/python -m wallex_sandbox.managed_trend <long-history-directory> --phase development
.venv/bin/python -m wallex_sandbox.managed_trend <long-history-directory> --phase validation
.venv/bin/python -m wallex_sandbox.managed_trend <long-history-directory> --phase check
.venv/bin/python -m wallex_sandbox.learned_returns <long-history-directory> --phase validation
.venv/bin/python -m wallex_sandbox.learned_returns <long-history-directory> --phase check
.venv/bin/python -m wallex_sandbox.learned_controls <long-history-directory>
.venv/bin/python -m wallex_sandbox.adaptive_returns <long-history-directory> --phase validation
.venv/bin/python -m wallex_sandbox.adaptive_returns <long-history-directory> --phase diagnostic
.venv/bin/python -m wallex_sandbox.report_managed_trend <long-history-directory>
.venv/bin/python -m wallex_sandbox.report_learned_returns <long-history-directory>
.venv/bin/python -m wallex_sandbox.report_adaptive_returns <long-history-directory>
.venv/bin/python -m wallex_sandbox.scan_triangular
```

The long collector reuses frozen public metadata and spread proxies, with three
concurrent public requests. It does not fetch or use account credentials.
Monthly/weekly model files are local joblib artifacts generated by these scripts;
load only trusted locally generated model files. Training/calibration and model
hashes are retained. Fixed experiment phases refuse to overwrite results.
The scanner reports quote upper bounds only, not paper fills or a win rate.

## Prospective fair-value strategy

See [FAIR_VALUE.md](FAIR_VALUE.md) for the new synchronized-quote paper runner,
cost checks, fee-lookup limitation and commands. It uses OKX/Bybit spot prices as
references, learns Wallex's normal local premium from prior observations, and
simulates only depth-aware Wallex fills. No new live-money broker is enabled.

## Scope

The bridge is a sandbox adapter, **not a native Hummingbot Wallex connector**.
`loop` selects Condor's unattended permission policy but dispatches only into
`FakeWallex`; Condor's `dry_run` policy refuses even those fake mutations.
The paper quote unit is TMN, although upstream risk messages may print `$`.
Those presentation/conversion assumptions require integration work before a real
Wallex connector is appropriate.

Passing these checks verifies the exercised plumbing. It does not establish a
trading edge, realistic fills, performance of a real LLM, or exchange-account
configuration. Real-money trading remains outside this sandbox's capabilities.
