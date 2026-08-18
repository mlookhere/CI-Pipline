# Repository engineering protocol

This repository is CI-Pipline, an Issue/PR/CI control plane for Claude Code. It is vendored
into other repositories, so every change here lands in someone else's gates. A guard that
reports success without checking is worse than no guard: it is the thing this repository
exists to prevent, and the thing it is most able to ship.

## Control model

- One independently deliverable change equals one controlling GitHub Issue, one task branch
  (`work/<issue>-slug`), and one PR into `dev`.
- The Issue is current handoff truth. The PR is review truth. CI and immutable build
  evidence are acceptance truth.
- Never create handoff documents; update the controlling Issue instead.

## Before mutation

1. Work on `work/ISSUE-slug`, never on `dev` or `master` directly.
2. Read the Issue's acceptance criteria, linked PR, and failing checks first.
3. Plan the smallest independently verifiable slice.
4. Add required risk labels before touching CI, security/auth, deployment, or dependency
   manifests (see `risk_paths` in `.claude-workflow.json`).

## Engineering behavior

- Trace the real execution path before editing. Prefer narrow reads and search over loading
  large files or logs.
- Do not retry the same failing command unchanged, weaken assertions, skip tests, add broad
  suppressions, lower thresholds, or widen permissions to make CI pass.
- Add regression coverage for behavior changes. Favor small explicit units, clear error
  contracts, bounded complexity, and observable failures.
- Never expose credentials, client data, or tokens to prompts, logs, Issues, artifacts, or
  subprocess environments. `.env` is never committed.
- A check that cannot fail is a defect. When adding or changing a gate, demonstrate it
  rejecting the thing it exists to reject, not only that it passes.
- What a consumer's repository looks like is configuration, not code. Nothing under
  `workflow/`, `ci/`, `scripts/` or `.claude/hooks/` may name a product, a package, a
  directory layout, or a dependency: read it from `.claude-workflow.json`, where the consumer
  owns it. `self_test.check_no_product_names` fails the gate if that slips.

## Gates

- During edits: targeted tests (`python -m pytest tests/test_x.py -q`).
- Before calling a change ready: `./ci/run fast`, then `./ci/run pr`.
- Pre-commit checks staged files; pre-push runs the fast gate. Neither replaces hosted CI.
  Run `./scripts/validate-workflow` after editing hooks, workflows, `ci/`, or
  `.claude-workflow.json`.
- AI review (`claude:review` label) is advisory. Deterministic checks and human risk judgment
  remain authoritative.

## Completion

- Claim completion only when the current commit has fresh gate evidence
  (`artifacts/ci/fast.json` for HEAD) and the Issue references that commit.
- Do not use `--no-verify`, force pushes, direct protected-branch pushes, or administrator
  merges. The command policy in `.claude/rules/default.rules` and branch protection enforce
  this; do not work around them.
