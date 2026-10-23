# Wallex — Kavoshgaran

Real-account Wallex bot and authenticated operator dashboard. The production
entry point is `python -m wallex_live`; the old static demonstration site has
been removed. Account data and order fills come from Wallex API responses.

- **Authorized capital:** 5,000,000 toman (50,000,000 rials), valued in USDT
  using a fresh USDT/TMN quote. One bot position uses at most 10% of this cap.
- **Final cutoff:** Monday 4 Aban 1405 / **26 October 2026, 23:50 Tehran**
  (**20:20 UTC**). New entries stop earlier to reserve a full holding window.
- **After cutoff:** cancel and reconcile account orders, then sell eligible
  non-fiat inventory into TMN and report the confirmed rial equivalent.
  Dust or failed conversions remain explicitly incomplete.
- **Target host:** https://wallex-kavoshgaran.liara.run
- **Liara team:** لیارا (pinned in the deployment configuration).

Hosting is verified live in the لیارا team. See the sanitized
[deployment status](ops/DEPLOYMENT_STATUS.json). The server-side Wallex checks
returned HTTP 401, so real orders and automatic liquidation remain disarmed.
No real orders were submitted during setup. Enabling trading requires a
verified account key and exact account scope. Hosting success does not mean
exchange trading readiness.

## Real service

See the [real-bot guide](wallex_live/README.md) for account routing, permissions,
capital controls, order reconciliation, persistence, and deployment.

The dashboard requires a private operator password. Exchange credentials and
account balances are never public. The Docker build copies only runtime code;
recording scripts, original key files, and research captures are excluded.

```bash
python -m pip install -r requirements-live.txt
python -m pytest -q wallex_live/test_live.py
# Set the secure environment described in wallex_live/README.md before startup.
python -m wallex_live
```

Push the intended revision to GitHub before deployment, then use
[ops/deploy_live.py](ops/deploy_live.py). It performs authenticated read-only
preflight checks and checks that the pushed revision matches before deployment.
Account scope is mandatory; credit accounts require the exact client ID and
verified permitted-market manifest.

## Video Script

[Video Script — real system](docs/VIDEO_SCRIPT_LIVE_FA.md) contains two Persian
recording plans with narration and timings of more than five minutes each.
Scripts live in GitHub only and are excluded from the hosted app. The
[earlier paper-research script](docs/VIDEO_SCRIPT_FA.md) is retained as history.

## Research evidence

The [fair-value hypothesis](wallex_sandbox/FAIR_VALUE.md) is unproven. Previous
research includes losing results and small positive samples; none establishes
profitable future trading. Historical paper reports retain their original labels.

- [Offline checks](wallex_sandbox/RESULTS.md)
- [Original public-data paper session](wallex_sandbox/LIVE_PAPER_RESULTS.md)
- [Indicator studies](wallex_sandbox/INDICATOR_RESULTS.md)
- [Continued research](wallex_sandbox/ADVANCED_RESEARCH_RESULTS.md)
- [Planning](planning/) and [market exclusions](wallex_sandbox/excluded-markets.json)

The full Condor source remains under its [MIT license](LICENSE). Its
[upstream README](README.condor.md) covers the separate upstream application.
