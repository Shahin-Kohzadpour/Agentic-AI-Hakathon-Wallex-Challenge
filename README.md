# Wallex Challenge — paper trading research

A reproducible, paper-only trading research harness built on
[Hummingbot Condor](https://github.com/hummingbot/condor). It includes public
Wallex market-data collection, simulated execution, Condor risk callbacks,
historical strategy comparisons, and a prospective fair-value experiment using
OKX and Bybit spot reference prices. The Wallex runners submit no exchange orders.

## Quickstart

Use Python 3.13 and [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r wallex_sandbox/requirements.lock.txt
.venv/bin/python -m pytest -q wallex_sandbox
.venv/bin/python -m wallex_sandbox.run
```

The offline runner uses synthetic prices and a fake wallet. For a short public-data
adapter check with no paper entries:

```bash
.venv/bin/python -m wallex_sandbox.fair_value_paper --observe-only --minutes 2
```

For the four-hour, 1,000-USDT simulated wallet:

```bash
.venv/bin/python -m wallex_sandbox.fair_value_paper --minutes 240
```

No API key is needed for these commands. The optional read-only account-fee probe
is documented separately. The account lookup returned HTTP 401; current paper
fees assume 0.2% per side. Ten trades is a ceiling, never a required quota.

## Source and results

- [Full harness guide](wallex_sandbox/README.md) and [fair-value strategy](wallex_sandbox/FAIR_VALUE.md).
- [Initial offline checks](wallex_sandbox/RESULTS.md), [original live-data paper results](wallex_sandbox/LIVE_PAPER_RESULTS.md), [indicator study](wallex_sandbox/INDICATOR_RESULTS.md), and [continued research](wallex_sandbox/ADVANCED_RESEARCH_RESULTS.md).
- [Compact result artifacts](wallex_sandbox/artifacts/), including reports, trade summaries, charts, model specifications, and compact configuration snapshots. Large raw candle captures and SQLite traces remain in the local ignored artifact store.
- [In-progress forward-session snapshot](wallex_sandbox/published_results/fairvalue_forward_20261023T121114Z/SNAPSHOT.md). This export does not update as the local process continues.
- [Challenge planning and open-source research](planning/), the [345-market exclusion manifest](wallex_sandbox/excluded-markets.json), and the [archived initial engine scaffold](legacy_engine/README.md).

The original ten forced short paper round trips lost about 5.30 USDT from 1,000,
with zero winning trades. Later historical experiments include positive and
negative results; none passed the research readiness gate. The prospective
fair-value run is unfinished in this snapshot. No profitable strategy or
completed ten-trade result is claimed for that run.

Historical files are retained as evidence, including failed experiments. Some
manifests contain original local paths; use the matching relative capture
directory when reproducing a command. Source hashes record the tested revision.
Imported model files are locally generated research artifacts; retraining
commands are in the harness guide. Runtime output stays ignored by default.

## Attribution and publication

The full Condor source is included under its existing [MIT license](LICENSE).
Its [original README](README.condor.md) describes the upstream application, whose
optional services and trading integrations are separate from this paper harness.
The Wallex sandbox suite passed 100 tests plus 21 subtests before publication.
The [CI template](ci/README.md) is included; enabling GitHub Actions requires
a token with workflow permission, which the publishing token does not have.

## Video Script

The Persian [Video Script](docs/VIDEO_SCRIPT_FA.md) contains two separate
recording plans, each designed for a six-to-seven-minute demo. It is a GitHub
document only and is not part of the Liara site's Docker build.

## Liara demo site

The [read-only demo site](liara_demo/README.md) presents the two experiments
without exposing Condor's authenticated dashboard or adding trade endpoints.
It has a health check at /health and runs on port 8080.

[PUBLICATION.json](PUBLICATION.json) records the exact source revision and export
time. This repository begins with a complete source snapshot. Credentials, the
original credential-containing zip, virtual environments, and runtime caches
are excluded.
