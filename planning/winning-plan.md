# Wallex challenge: plan to compete for first place

Status: initial plan based on all three supplied files and official Wallex documentation checked on 2026-10-22. Strategy profitability, account configuration, actual fees, and challenge dates remain unverified. The credential is intentionally absent from these documents. No authenticated account calls or trades were made during planning.

## 1. What we need to optimize

Build a reliable autonomous trading engine with a demonstrable trading edge, genuine AI involvement, controlled losses, and a complete evidence package. Start with one strategy and a small eligible market universe. A terminal report is sufficient for the demo.

The supplied rubric is explicitly described as a proposed allocation:

| Criterion | Points | Evidence to produce |
| --- | ---: | --- |
| Final net profit ranking | 90 | Exchange-reconciled final TMN balance and return |
| Drawdown control | 30 | Continuous account equity and maximum drawdown |
| Reasonable activity | 15 | Genuine qualifying opportunities and resulting fills |
| Runtime stability | 15 | Uptime, order errors, recovery and reconciliation records |
| Strategy logic | 40 | Clear hypothesis, entry/exit logic, cost assumptions |
| Real AI use | 35 | Saved model decisions, tool calls, comparison with AI disabled |
| Risk and capital management | 30 | Position limits, loss limits, enforced risk vetoes |
| Reproducibility | 25 | Locked environment, code/model/config/data versions, replay |
| Backtest and shadow test | 20 | Chronological evaluation and recorded shadow results |

Profit ranking accounts for 90 points; the remaining criteria account for 210. However, the text also describes a financial ranking followed by technical review of leading teams. Technical points alone may not overcome a low financial rank. Ask which selection procedure is binding.

Mandatory: approximately five-minute demo video, source code matching the version that traded, a Markdown technical document in the private repository, and decision logs explaining every trade. Missing the video explicitly disqualifies the submission.

Do not manufacture activity. An explainable abstention is preferable to paying fees for a trade without an edge. Winning cannot be established from the brief alone; the first experiments must test whether an edge survives actual costs.

## 2. Rules, conflicts, and working assumptions

The full organizer questions are in `organizer-questions.md`.

1. **End-of-challenge valuation conflict:** lines 134–141 of `wallex-p1.txt` include remaining assets and unrealized P&L; line 142 requires realization in toman. Working assumption: cancel orders and liquidate positions into TMN before the deadline. Clarify treatment of USDT and untradeable dust.
2. **Ranking uncertainty:** the two-stage ranking description and proposed 300-point rubric do not fully define how winners are selected. Confirm weights, shortlist rules, tie-breaks, and activity expectations.
3. **Arithmetic error:** the example at lines 146–150 gives initial capital of 5,000,000 and final value of 6,250,000, correctly calls that 25%, but incorrectly calls the profit 1,500,000. Correct profit is 1,250,000 TMN. Implement the formula, not the erroneous subtraction.
4. **Dates:** three days are specified, but exact opening/closing timestamps and submission cutoff are absent. All timing below is relative until confirmed.
5. **Credit account:** do not infer that the supplied key belongs to the correct funded account. Verify through read-only account queries during implementation. Credit-account spot trading requires the account-specific key and `sub-account-client-id` header. [Official spot documentation](https://developers.wallex.ir/docs/spot-intro).
6. **AI definition:** coding with AI does not demonstrate AI making runtime trading decisions. Ask whether a learned predictor plus an autonomous decision agent satisfies the rubric, and whether any LLM or MCP requirement exists. Neither appears mandatory in the supplied tool descriptions.

The constraints file excludes **345 unique pairs**. `excluded-markets.json` contains the complete parsed set and source checksum. Exclusions apply to exact base/quote pairs; a banned TMN pair does not imply an unlisted USDT pair is banned, or vice versa.

An initial public `GET /v1/markets` snapshot contained 373 listed pairs, of which 120 survived the supplied exclusions. The captured metadata and quotes are in `public-market-candidates.json`, with a capture timestamp. These are research candidates, not verified account permissions or evidence of current liquidity. Some excluded symbols were absent from the public listing; they remain excluded if later listed.

At runtime, eligibility must be the intersection of exchange-active markets, credit-account-permitted markets, and our explicit strategy universe, minus all supplied exclusions. Reject unknown or unmapped symbols. Recheck before each order and after reconnects. Public listings alone cannot establish credit-account permission.

## 3. First experiments: establish feasibility

Before choosing coins or tuning indicators:

1. Confirm schedule, account identity, account/sub-account routing, initial equity, existing holdings and orders, and credential expiration. Keep all credentials local and excluded from source control and model prompts.
2. Read the account's actual maker/taker fees for candidate pairs. Verify minimum notional, minimum quantity, precision, price ticks, quantity steps, supported order types, and how fees reduce received inventory. Interpret metadata fields from documented semantics; do not blindly treat every numeric field as an increment.
3. Collect historical Wallex candles and current books/trades. Measure missing data, stale quotes, spreads, depth, and estimated execution cost at our proposed order sizes. Aim for 60–180 days of usable history where available; record actual coverage rather than claiming the target was obtained.
4. Rank a small candidate universe by tradability and measured cost, then select approximately 3–5 pairs. Prefer TMN quote markets initially to simplify settlement and avoid an extra conversion leg, provided measured liquidity supports them. BTC/TMN and ETH/TMN are examples to verify, not confirmed eligible or recommended purchases.
5. Start recording live data immediately once the collector exists. Historical candle backtests cannot establish order-book execution quality.

Official discovery: candles are available at `GET /v1/udf/history`; fees at `GET /v1/account/fee`. The documented candle resolutions include 1 and 15 minutes, but not 5 minutes; aggregate complete 1-minute candles if a 5-minute series is needed. [Candles](https://developers.wallex.ir/docs/basic-services-candles), [account fees](https://developers.wallex.ir/docs/basic-services-account-fee).

Cost hurdle for a complete round trip:

`expected net return = expected gross return - entry fee - exit fee - spread/impact - slippage - applicable financing/conversion costs`

Use a consistent convention: if bid/ask execution prices already account for spread, do not subtract that spread again. Only trade when conservative expected net return exceeds a preregistered uncertainty buffer. A high classification accuracy does not establish profitability.

## 4. Strategy hypothesis and meaningful AI

**First hypothesis to test:** long-only intraday momentum with a learned filter that estimates whether a candidate move can cover its costs. Cash is the default alternative. No additional leverage or shorting in the initial implementation. The allocated credit is already part of the account setup; clarify any financing charge separately.

Use completed 15-minute candles initially, with a bounded holding horizon of roughly 30–120 minutes as a research range. Fix the final horizon and parameters using training/validation data before the final holdout. Features can include past returns, realized volatility, volume surprise, trend strength, and broad market movement. Every feature must have been observable at decision time. Use live spread/depth as an execution filter; only include historical book features in training if actual point-in-time data exists.

Start with a regularized, interpretable learned model. One workable target is the distribution of forward gross returns over the chosen horizon, allowing expected value to be estimated before subtracting measured trading costs. Freeze the label, objective, calibration, and policy before evaluating the holdout. Compare with cash and the same momentum strategy without AI; use buy-and-hold only as a diagnostic benchmark. Keep a mean-reversion strategy as a later challenger only if the first hypothesis fails and time permits.

Recommended runtime agent design:

- The predictor scores eligible opportunities using a saved model artifact.
- A bounded decision agent can read recent feature summaries, model scores, cost estimates, portfolio state, and relevant recorded performance. It chooses among already eligible candidates or abstains, returning structured reasons and validity time.
- Its selection must change actual entry decisions to count as a meaningful runtime role. Store accepted and rejected candidates and compare the resulting policy with the predictor-only baseline during shadow testing.
- Hard risk checks independently veto every proposed action. The agent cannot add a banned market, enlarge limits, change code, or directly access the exchange credential.
- Missing, stale, malformed, or timed-out AI output blocks new entries. Existing positions retain deterministic exit and recovery logic.

Run the decision agent on the strategy cadence, rather than on each market tick. Use a configured model provider with a fixed budget. Keep prompts, tool inputs/results, model identifiers, outputs, and timestamps. Do not use future knowledge or present-day news in historical tests. Historical results for the predictor-only policy must be labeled as such; use recorded shadow sessions to evaluate the complete runtime agent.

Do not spend the event building a large agent framework. One orchestrator with narrowly scoped tools is sufficient. Retain the runtime agent only after its operational behavior is tested; document any version change and its effect on results.

## 5. Engine architecture and order correctness

Use one maintainable Python application with replaceable brokers for historical replay, shadow simulation, and live exchange execution. One feature/decision/risk path serves all modes. A local durable event store holds decisions, order intents, fills and reconciliation; compressed files hold market data. Pin exact dependencies when implementation begins.

`market data -> validated completed features -> AI decision -> deterministic risk checks -> durable order intent -> broker -> reconciliation -> portfolio/metrics`

Essential implementation work:

- Use decimal arithmetic for balances, order sizes and fees. Validate TMN versus IRR explicitly; 5,000,000 TMN equals 50,000,000 IRR, but the API's actual unit must be checked.
- Persist a unique client order ID and intended quantity before sending. On an ambiguous timeout, query that ID before any resend. A fresh ID must not create a duplicate exposure.
- Track submitted, open, partially filled, filled, cancel-requested, canceled, rejected and unknown states. A cancellation request is not confirmation; reconcile fills that arrive during cancellation.
- Track filled inventory net of asset-denominated fees. Reserve inventory for existing sells. Never infer a fill from a submitted order or an order touching a candle's price.
- Reconcile balances, fills and open orders at startup and regularly. Permit one active execution writer; restarts recover state before new entries.
- Check existing orders for self-crossing before entry/exit placement. Cancel and confirm conflicting orders. No use of teammate accounts for liquidity or matching.
- Detect stale/gapped market streams. Pause entries, obtain fresh state, and reconcile before resuming. Use bounded backoff, endpoint-specific rate limits, and durable error logs.
- Begin with simple limit execution and explicit timeout/repricing rules. Do not assume post-only, IOC, or atomic bracket orders exist. Use a separate bounded emergency exit policy; stops can slip or fail to fill.

Wallex documents unique `client_id` values and a placement limit of 20 requests per 10 seconds. [Order creation](https://developers.wallex.ir/docs/spot-create-order).

For new WebSocket work, Wallex recommends v2 at `wss://api.wallex.ir/connection/websocket`. Public non-candle channels do not replay missed events or provide an initial snapshot on subscribe. Resynchronize using fresh state and verify channel payload semantics before treating book updates as snapshots or deltas. [WebSocket documentation](https://developers.wallex.ir/docs/socket-intro).

Direct API execution provides a small auditable path. MCP is an optional tool interface, pending verification of official endpoint, authentication, and available methods; do not assume an MCP service is configured simply because the brief mentions it.

## 6. Initial risk policy to validate

These are proposed starting limits, not exchange rules or evidence of optimal settings. Change them only through versioned configuration and validation.

| Limit | Initial proposal |
| --- | --- |
| Initial funded deployment | One position with at most 10% equity, subject to minimum order size |
| Risk budget after operational validation | At most 0.25% equity per trade; initially half that |
| Per-position notional ceiling | 20% equity after validation |
| Total gross exposure ceiling | 40% equity; initially one position |
| Highly correlated exposure | Aggregate cap, initially 30% equity |
| Daily equity loss threshold | 2%: stop new entries; follow predetermined reduction/exit policy |
| Peak-to-trough drawdown threshold | 4%: cancel entries, reconcile, and invoke the tested exit policy |
| Unknown order/account state | Block entries until reconciled |
| Stale data, excessive spread, or insufficient depth | Block entries |

Size from both stop distance and costs:

`notional <= min(position cap, remaining portfolio cap, risk budget / (stop fraction + estimated fees/slippage))`

At initial equity, 0.25% is 12,500 TMN and a 20% notional cap is 1,000,000 TMN. Reject opportunities that cannot satisfy both minimum order size and risk limits. Stops express intended risk; they cannot guarantee a maximum realized loss during gaps, outages, or illiquidity.

Track realized P&L and marked-to-market equity separately. Drawdown must include open inventory, fees, reserved balances, and financing where applicable. Freeze thresholds before deployment; do not loosen limits to recover losses.

## 7. Validation gates and 72-hour schedule

This schedule assumes the full three-day window remains. Compress only after actual dates and existing code are known.

| Relative time | Work | Exit condition |
| --- | --- | --- |
| Hours 0–3 | Resolve account/rule questions; start collector; define interfaces | Correct account and units understood; eligibility/cost inventory available |
| Hours 3–10 | Broker, ledger, reconciliation, historical dataset and baseline | Recovery and partial-fill scenarios pass; usable data coverage recorded |
| Hours 10–18 | Learned filter and chronological backtests | Reproducible out-of-sample results, explicit costs and uncertainty |
| Hours 18–26 | Full-stack shadow run and fault injection | Operational gate passes; reconcile simulated execution assumptions |
| Hours 26–60 | Small initial live deployment, only after gates pass | Measured fills/costs agree sufficiently with policy; maintain shadow comparison |
| Hours 60–end | Freeze final deployment, prepare evidence/video, wind down | Final TMN settlement, verified balances, complete submission |

Begin shadow operation as early as components permit; it need not wait for all research. Reserve live time only if the evidence supports it. If strategy validation fails, reduce scope or evaluate the preregistered challenger; do not claim a trading edge or create forced trades to fill the schedule.

Research gate:

- Use chronological walk-forward splits. Purge training samples whose forward labels overlap validation/test periods; fit preprocessing and calibration only inside training splits. Do not randomly split time-series rows.
- Use the same eligible universe and recorded cost conventions. Report any historical eligibility uncertainty and survivorship bias.
- Test delayed execution, higher fees/slippage, missed fills, and parameter neighborhoods. Limit the search and save every trial, including failures.
- Advance only with positive net out-of-sample expectancy supported across multiple periods and enough independent trades to assess uncertainty. Set minimum evidence criteria before viewing the final holdout. If the sample is too small, report uncertainty and remain in shadow.
- For candle-only simulation, use conservative next-observation fills. If stop and target both occur in one candle, use adverse ordering or finer data. Never present candle fills as measured queue performance.

Operational gate:

- No prohibited orders, duplicates, unexplained balances or unresolved orders in shadow/fault scenarios.
- Timeout-after-acceptance, partial fill during cancellation, restart with an open position, stale feed, AI outage, rate limiting, and liquidation scheduling all produce the documented behavior.
- Record decision and order latencies, simulated cost, fill uncertainty, uptime, and reconciliation failures. Several hours of shadow establish operational evidence, not statistical proof of profitability.

First live orders remain an explicit subsequent execution step after this planning task. The presence of the key is not an instruction to place orders now.

## 8. Deadline, evidence, and presentation

Make the official deadline a required timezone-aware configuration value. Refuse live startup if it is missing or already passed.

Stop new entries at least one maximum holding horizon plus the exit buffer before the deadline. Proposed exit buffer: 60 minutes, adjusted for measured liquidity. At T−60 minutes, cancel entries and execute the predetermined liquidation policy; by T−30, reconcile fills and residual balances and handle dust under the organizer's rule. Keep checking through T. A submitted sell does not establish liquidation.

For every decision, record: event and receive times; completed input data IDs; features; market eligibility; fees and cost estimate; model/prompt/config/code versions; candidate scores; structured agent output; action or abstention reason; risk checks; order intent/client ID; exchange acknowledgement; fills/fees; resulting balances. Record operator interventions and their reason without treating manual trades as algorithmic performance.

Replay two things separately: exact recorded decisions using saved model/agent outputs, and a fresh recomputation of the frozen deterministic parts. Fresh LLM responses and future market fills cannot be guaranteed identical. Keep a manifest mapping every live interval to its exact code/config/model version so the delivered source covers the code that produced the return.

Final package:

- Runnable private repository with a lockfile, setup instructions, and one-command replay/report generation.
- Markdown explanation of architecture, AI role, entry/exit policy, risk, fees, tests, limitations and final version manifest.
- Equity/drawdown plots, realized net TMN P&L, exposure, trades and volume, actual fees, slippage, uptime and failures; clearly separate backtest, shadow and live results.
- Sanitized decision/fill logs and data/model hashes, plus an agreed secure way to provide private account evidence to judges.
- Five-minute video: 45 seconds on the problem/approach, 60 on real AI decisions, 60 on testing, 75 on live results/risk/recovery, and 60 on replay/final results. Never display credentials.

The GitHub connector returned 404 for the requested private repository during planning. Its contents and access are unverified; no remote changes were made. Keep planning local until repository access is available. See `agent-briefs.md` for implementation handoffs.
