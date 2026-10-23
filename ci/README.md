# GitHub Actions template

`wallex-paper.yml` contains the paper harness workflow. The publishing OAuth token
does not have GitHub's `workflow` scope, so GitHub rejected creating an active
`.github/workflows/ci.yml`. The full configuration is preserved here.

To enable it later with suitable GitHub permissions, place this template at
`.github/workflows/ci.yml`. Local publication checks passed 100 tests and 21 subtests.
The original upstream workflow is preserved at `../.github/upstream-ci.yml.example`.
