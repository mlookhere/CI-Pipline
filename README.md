# CI-Pipline

A GitHub Issue/PR/Actions control plane driven by **ChatGPT through an authorized GitHub MCP connection**. Hosted jobs are deterministic and headless; there is no second coding agent or model API key.

GitHub is the durable source of truth. Each independently deliverable change maps to one Issue, one work branch based on **dev**, and one PR into **dev**.

## What it contains

| Path | Role |
| --- | --- |
| .chatfreept/ci.json | Headless test, build, security, and quality stages |
| .chatfreept/github.json | Branch, Issue, risk labels, and repository policy |
| ci/run | Cross-platform deterministic stage runner, writing evidence under artifacts/ci/ |
| ci/quality.py | Changed-file, secret, complexity, and quality checks |
| workflow/chatfreept_checks.py | Fail-closed reconciliation of exact GitHub checks and provenance |
| workflow/chatfreept_control.py | Deterministic GitHub control-Issue synchronization |
| workflow/chatfreept_runtime_audit.py | Blocks legacy agent assets and forbidden executable references |
| workflow/pipeline_sync.py | Conflict-aware ownership-based consumer updates |
| .github/workflows/ | Required PR, release, security, and nightly checks plus control sync |

**Branch note:** dev is the integration branch. The current policy declares **master** as production; verify actual GitHub settings before making any branch change. Do not change the default branch or force-push as part of migration.

## Run deterministic checks

~~~bash
./scripts/bootstrap --ci
./ci/run fast
./ci/run pr
./ci/run audit
~~~

The fast stage validates the CI control plane and vendor runtime, quality, formatting, lint, and types. The PR stage runs tests and coverage. Every configured command must perform real, headless work; missing commands and empty stages fail closed.

Local success is not merge authorization. Inspect the exact PR head and all **five required GitHub Actions checks**. Missing, skipped, pending, or failed required jobs are never green.

## Install into a consumer

Start with a trusted local checkout of this repository and a separately configured consumer project:

~~~bash
./scripts/pipeline-sync --dry-run --upstream /path/to/CI-Pipline --consumer /path/to/app
./scripts/pipeline-sync --install --upstream /path/to/CI-Pipline --consumer /path/to/app
./scripts/pipeline-sync --check --upstream /path/to/CI-Pipline --consumer /path/to/app
~~~

Only reviewed runtime-neutral CI assets are owned. The installer preserves consumer extras and ignores, refuses symlink/conflicting targets, and never silently overwrites custom code. Configure project-specific headless commands separately, and validate the consumer's own Actions before claiming parity. See [consumer sync](docs/CHATFREEPT_SYNC.md).

## Repository engineering protocol

- ChatGPT uses connected GitHub tools to read and mutate Issues, branches, PRs, and Actions; GitHub Actions never runs another coding agent.
- Keep the Issue as handoff truth, the PR as review truth, and immutable exact-head Actions results as acceptance truth.
- Never push directly to protected branches, force-push, bypass required checks, lower thresholds, disable tests, or use administrator merges.
- Diagnose the first causal failure from actual Actions logs and add regression tests. Never treat zero checks, missing jobs, or unavailable branch-protection reads as success.
- Keep tokens, credentials, .env files, and private client data out of commits, prompts, Issues, logs, and artifacts.

This repository is a vendorable CI plane, not a deployable application. See the [operating contract](docs/CHATFREEPT_CI.md), [master plan](docs/MASTER_PLAN.md), [check provenance](docs/CHATFREEPT_CHECKS.md), and [failure diagnostics](docs/CHATFREEPT_DIAGNOSTICS.md).
