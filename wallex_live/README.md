# Real Wallex bot

This service uses authenticated Wallex account endpoints and exchange order
receipts. It does not create simulated balances or simulated fills.

- Authorized capital: **5,000,000 TMN = 50,000,000 IRR**, traded from available
  USDT with a fresh USDT/TMN quote. One position is capped at 10% of that budget.
- Final cutoff: **Monday 4 Aban 1405 / 26 October 2026, 23:50 Asia/Tehran**,
  which is **20:20 UTC**.
- New entries stop one maximum holding horizon plus a five-minute buffer before
  the cutoff. The normal holding horizon is two hours.
- At/after the cutoff, cancel active account orders, reconcile actual execution,
  and sell the selected account's non-fiat inventory through eligible TMN
  markets, or eligible USDT routes followed by USDTTMN. There are no withdrawals.
- Wallex settles these markets in **toman**, so the dashboard reports rial as
  confirmed TMN balance multiplied by ten. It does not relabel a USDT balance.
- Dust, locked funds, excluded/unavailable markets, missing liquidity, rejected
  orders, and uncertain submissions remain visible as incomplete settlement.
  A submitted or acknowledged sell is never treated as a confirmed conversion.

## Account and execution

The account scope is mandatory: main or credit. Credit accounts additionally
require the exact sub-account client ID and a verified allowed-market manifest.
The supplied challenge exclusions apply to both new entries and settlement
routes. Without eligible routes, the system reports blocked residuals.

The entry rule is the existing prospective fair-value research hypothesis:
synchronized Wallex/OKX/Bybit spot references, prior local-basis median/MAD,
calibration, persistence, and a cost-qualified opportunity. It remains an
unproven strategy. There is no paid LLM call or claim of profitable performance.
The real implementation uses verified account fee rates and actual order fills.
Orders are bounded LIMIT orders. There is one bot position, a maximum of ten
completed round trips, a cooldown, a one-percent position stop trigger, and a
two-percent realized budget-loss entry limit. Stops cannot guarantee fill prices.

Before the cutoff the bot exits only the quantity it acquired. At the requested
final liquidation, all assets in the explicitly selected account are in scope.
Do not run a second independent trader on the same account: external orders
block new bot entries, and all active orders are canceled during final settlement.

Order intents and client IDs are committed to SQLite before submission. A
timeout after a possible acceptance never produces an automatic duplicate POST.
Uncertain orders are reconciled by client ID; if the result remains unknown,
operator review is required. State is bound to the account scope/key fingerprint.
Changing keys requires a reviewed state migration, not deleting the journal.

## Service configuration

Required secure environment values:

    WALLEX_API_KEY
    WALLEX_ACCOUNT_TYPE=main or credit
    WALLEX_SUB_ACCOUNT_CLIENT_ID=required for credit only
    WALLEX_CREDIT_ALLOWED_MARKETS=JSON array required for credit trading
    WALLEX_DASHBOARD_PASSWORD=random password of at least 20 characters
    WALLEX_ENABLE_LIVE_ORDERS=1
    WALLEX_DATA_DIR=/data

Live orders are disarmed unless explicitly enabled. Missing account scope, bad
authentication, unreconciled balances/orders, or missing fees blocks execution.
An HTTP 401 is shown as authentication failure, never as an empty wallet.

Run one replica with a persistent disk mounted at /data. The service enforces a
single writer and keeps the journal across restart. A hosting outage, invalid
key, or unavailable exchange can delay liquidation; the system cannot guarantee
that every asset will be convertible at the exact cutoff.

The dashboard uses HTTPS plus HTTP Basic authentication (username: operator).
Account balances and logs require that authentication. Public /health checks
only web-process liveness; /ready returns 503 unless the live account loop is
connected and current. No web route places trades or exposes exchange keys.

## Deployment

Commit and push all changes first. Then run the deployment helper with the
explicit account scope. It verifies the GitHub revision and read-only Wallex
access before provisioning/enabling the real worker:

    python ops/deploy_live.py --key-file "<local key file>" --account-type main

For a credit account also provide --client-id and --credit-markets-file.
The helper creates wallex-kavoshgaran and a persistent wallex-state disk,
sets secrets in memory through Liara's authenticated API, deploys the pushed
revision, then verifies the public endpoint and authenticated real-account state.
It never places keys in command arguments, source code, Docker layers, or logs.

Local verification uses controlled fake transports only:

    python -m pytest -q wallex_live/test_live.py

Official API sources:
[account scope](https://developers.wallex.ir/docs/spot-intro),
[main wallet](https://developers.wallex.ir/docs/basic-services-main-wallet),
[orders](https://developers.wallex.ir/docs/spot-create-order),
[order reconciliation](https://developers.wallex.ir/docs/spot-single-order-detail),
[cancellation](https://developers.wallex.ir/docs/spot-cancel-order).
