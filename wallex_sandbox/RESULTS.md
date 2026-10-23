# Offline Condor sandbox results

Run: 2026-10-23T05:32:56.579798+00:00

- Condor source revision: `8e4860fa3d7efd4b55c2b74dd7f1f439e5a4f32c`.
- Python: 3.13.15.
- Wallex sandbox checks: **36 passed**, zero failures/errors.
- Selected upstream Condor checks: **225 passed**, zero failures/errors/skips.
- Total: **261 passed**.
- IP connections blocked during checks.
- Exchange credentials used: **none**.
- Model: **scripted responses**, no paid or remote LLM calls.
- Exchange: **local fake wallet**, no Wallex connection or orders.

Results cover the real Condor permission/risk gate and actual tick orchestration
with fake model responses, plus the custom fake-exchange bridge. They do not
measure trading profitability or establish a production Wallex connector.

Full machine-readable report: `artifacts/checks_20261023T053242757879Z/summary.json`.
Verbose log and upstream JUnit XML are in the same directory.

Rerun from the Condor checkout:

```bash
.venv/bin/python -m wallex_sandbox.checks --upstream
.venv/bin/python -m wallex_sandbox.run
```
