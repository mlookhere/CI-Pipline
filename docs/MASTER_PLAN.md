# MASTER PLAN — ChatGPT-native CI control plane

> Status: **planned, not yet implemented**. GitHub repository `mlookhere/CI-Pipline`; roadmap control [#95](https://github.com/mlookhere/CI-Pipline/issues/95). Each slice has its own Issue, branch from `dev`, pull request into `dev` and complete required Actions evidence.

## Repository-grounded baseline (2026-10-18)

- **Public**, writable repository; integration/default branch `dev@d6ce65c73c05f61d3f71510c73c35ab7d109b82a`. Production is protected `master@6971739c52c9db92a5ee65a2b080c2618f138df6`; **no `main` branch exists**. Do not silently rename or change the default branch.
- Required checks on `dev`: `PR metadata`, `Fast deterministic gates`, `PR test/build gates`, `Dependency audit`, `Workflow policy`. Required checks on `master`: `Release metadata`, `Full regression and production build`, `Workflow policy`. These names are an observed baseline, not permission to treat an unobserved check as passed.
- At the start of modernization, bootstrap and the CI runner depended on the legacy configuration. Existing CI contained useful deterministic PR, release, security, nightly and control-sync workflows, but `.claude/`, `CLAUDE.md`, `flow` internals and optional Claude advisory review are **not** ChatGPT-native.
- Pre-existing backlog: [#59](https://github.com/mlookhere/CI-Pipline/issues/59) consumer sync divergence, [#60](https://github.com/mlookhere/CI-Pipline/issues/60) release size labeling, [#10](https://github.com/mlookhere/CI-Pipline/issues/10) coverage. Their narrow bug fixes stay on the original Issues; the new slices supply integration work without closing or duplicating those records.
- Nine existing Dependabot pull requests must be inspected individually before merging or closing; never merge a stale dependency change solely as cleanup.

## Goal and architecture

ChatGPT, using the authorized GitHub MCP connector, is the **only autonomous engineering agent**. GitHub Issues, branch heads, PRs, protected checks and Actions logs are durable truth. GitHub Actions runs deterministic, headless builds/tests/security gates; it does **not** secretly run an AI coding agent. Chat FreePT may initiate the next interactive ChatGPT continuation, but no CI check depends on that continuation occurring. No Claude commands, hooks, secrets, or review agents may be copied into a consumer.

Keep the KISS contract: one Issue → one `work/<number>-<slug>` branch → one PR into `dev` → verified required checks → merge. Production promotion is a separate `dev`→protected production PR with its own release Issue. Preserve current gates while replacing their implementation incrementally. A stage that ran no meaningful command fails.

### Gate names and evidence

Every integration PR must satisfy all 5 currently required checks: `PR metadata`, `Fast deterministic gates`, `PR test/build gates`, `Dependency audit`, `Workflow policy`. Production promotion requires `Release metadata`, `Full regression and production build`, `Workflow policy`. The reviewer verifies **actual head SHA**, job status/conclusion, and branch-protection required set. Local command results are useful diagnostics but are not substitutes for GitHub Actions. Zero, missing, skipped-required, queued, pending, cancelled, red, or stale evidence is not green. Merge-queue checks are reconciled against the proper queue SHA.

## Ordered independently deliverable slices

### 1. [ChatGPT scaffold and executable CI baseline](https://github.com/mlookhere/CI-Pipline/issues/96) — #96

**Areas:** `.chatfreept/project.json`, `docs/CHATFREEPT_CI.md`, `scripts/bootstrap`, `ci/run*`, `workflow/self_test.py`, tests  
**Risk labels:** `type:feature`, `risk:ci`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Preserve `dev` as integration and protected `master` as production while documenting the requested future `main` name.
- [ ] Validate the versioned ChatGPT project manifest and verify the real required-check names.
- [ ] Prove `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr` execute real commands and fail if stages are empty.
- [ ] CI PR has all five required `dev` jobs completed successfully.


### 2. [Runtime-neutral CI stages and cross-platform runners](https://github.com/mlookhere/CI-Pipline/issues/97) — #97

**Areas:** `ci/`, `workflow/`, `scripts/`, `.chatfreept/`, tests  
**Risk labels:** `type:feature`, `risk:ci`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Use a versioned runtime-neutral command manifest with strict validation, not Claude hooks, as source for CI commands.
- [ ] Keep bootstrap and stage entry points usable headlessly on GitHub runners and supported developer platforms.
- [ ] Reject unknown/empty stage commands, placeholders, and missing build or test executables without success-by-default.
- [ ] Compare legacy and new stage behavior with regression fixtures before cutover.


### 3. [GitHub MCP durable state and restart-safe handoffs](https://github.com/mlookhere/CI-Pipline/issues/98) — #98

**Areas:** `workflow/`, `.chatfreept/`, `docs/`, tests  
**Risk labels:** `type:feature`, `risk:ci`, `risk:security`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Write the state-machine contract from Issue to branch to PR to checked merge and release.
- [ ] Make evidence and state transitions idempotent and traceable to exact branch SHA, Issue, PR and check run.
- [ ] Reject stale writers, conflicting state, wrong repository, missing refs and replay duplicates.
- [ ] Recover from resumed chats using GitHub without relying on conversation memory or background agents.


### 4. [Fail-closed check reconciliation and provenance](https://github.com/mlookhere/CI-Pipline/issues/99) — #99

**Areas:** `workflow/validate_pr.py`, `workflow/check_workflow_policy.py`, Actions definitions, tests  
**Risk labels:** `type:feature`, `risk:ci`, `risk:security`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Cross-check configured required job names against actual protected branch requirements and exact-head-sha run evidence.
- [ ] Block merge on zero, missing, pending, cancelled, failed, skipped-required or stale required checks.
- [ ] Preserve five dev and three production required check gates, including merge-queue path.
- [ ] Test mixed-status, rerun, missing API fields and head-moved scenarios.


### 5. [Autonomous diagnostics and bounded recovery](https://github.com/mlookhere/CI-Pipline/issues/100) — #100

**Areas:** `workflow/`, `ci/`, `.chatfreept/`, tests  
**Risk labels:** `type:feature`, `risk:ci`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Extract the first causal failing step from job logs; capture bounded, redacted excerpts with an Actions URL.
- [ ] Track the failure fingerprint and stop after three unsuccessful attempted fixes for the same cause.
- [ ] Do not spin waiting for CI: persist a resumable pending state and recheck on the next invocation.
- [ ] Exercise rate limits, missing logs, log redaction, retries and concurrent reruns in tests.


### 6. [Consumer vendoring and intentional-divergence sync](https://github.com/mlookhere/CI-Pipline/issues/101) — #101

**Areas:** `workflow/pipeline_sync.py`, `scripts/pipeline-sync`, `docs/`, tests  
**Risk labels:** `type:feature`, `risk:ci`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Sync explicitly owned runtime-neutral assets, with dry-run and check modes; exclude `.claude/` and other Claude-only assets.
- [ ] Resolve the intentionally divergent consumer drift behavior tracked in [#59](https://github.com/mlookhere/CI-Pipline/issues/59) without hiding real upstream drift.
- [ ] Preserve product-specific commands, deploy workflows, secrets and non-owned files.
- [ ] Use golden tests for two consumers, deliberate additions, conflicting owned files, idempotence and sync/no-op.


### 7. [Remove Claude-only runtime and restrict agent execution](https://github.com/mlookhere/CI-Pipline/issues/102) — #102

**Areas:** `.claude/`, `.claude-workflow.json`, `CLAUDE.md`, `.github/claude/`, optional review action, `flow`, workflow checks, Dependabot  
**Risk labels:** `type:maintenance`, `risk:ci`, `risk:security`, `risk:dependencies`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Replace legacy dependencies only after runtime-neutral parity and new tests pass; remove obsolete files/actions/keys/label references.
- [ ] Prohibit Anthropic/Claude-dependent active workflows or vendored content; ChatGPT with authorized GitHub MCP is the only autonomous agent.
- [ ] Keep deterministic CI and protected required jobs intact; no generic policy gate is dropped.
- [ ] Test against accidental reintroduction of legacy hooks or external agent runtimes.


### 8. [Regression, security and supply-chain hardening](https://github.com/mlookhere/CI-Pipline/issues/103) — #103

**Areas:** `tests/`, `workflow/`, `ci/requirements-ci.txt`, `.github/workflows/security.yml`, nightly and tool configs  
**Risk labels:** `type:maintenance`, `risk:ci`, `risk:security`, `risk:dependencies`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below.

**Acceptance criteria**

- [ ] Expand coverage for lifecycles, runners, policy and fail-closed boundaries; reconcile old [#10](https://github.com/mlookhere/CI-Pipline/issues/10).
- [ ] Raise coverage only after measured passing evidence; never lower pre-existing floors.
- [ ] Check pinned Actions, least privilege, secrets, dependency audits, SBOM and unsafe workflow patterns.
- [ ] Fast, PR, release and nightly stages must produce nonempty, accurate, commit-specific evidence.


### 9. [Release promotion and safe production-branch alignment](https://github.com/mlookhere/CI-Pipline/issues/104) — #104

**Areas:** `workflow/release_manifest.py`, `workflow/validate_pr.py`, `ci-release.yml`, `.chatfreept/`, release docs and tests  
**Risk labels:** `type:feature`, `risk:ci`, `risk:deployment`  
**Required gates:** `./scripts/bootstrap --ci`, `./ci/run fast`, `./ci/run pr`; all five protected `dev` checks on the slice PR, plus tests for the behaviors below. Promotion additionally requires all three protected production checks before release merge.

**Acceptance criteria**

- [ ] Build `dev`→production release PRs only with complete metadata, diff summary, provenance and rollback.
- [ ] Resolve automatic large-change labels on release Issue and PR via existing [#60](https://github.com/mlookhere/CI-Pipline/issues/60), without changing size budgets.
- [ ] Do not rename or bypass protected `master`; introduce `main` only after equivalent required checks and protections are verified and any missing administrative decision is resolved.
- [ ] Verify production's three required Actions checks and open plan Issue/PR state before completion.

## Rollout dependencies and risk isolation

1. Slice 1 starts from existing executable CI; it does not replace the five protected job names. Slice 2 migrates command configuration only after equivalence tests. Slice 3 establishes durable handoffs; slices 4–5 add exact check reconciliation and actionable diagnostics.
2. Slice 6 updates the vendored consumer contract without mixing consumer applications into this repository. Slice 7 deletes Claude-only runtime **after** policy/check parity is established. Slice 8 provides measured test and security assurance. Slice 9 finishes the release cycle.
3. `master` remains the production branch unless a separate, fully protected `main` is configured and verified. Never force-push or change the repository default branch as part of migration. If branch administration tooling is unavailable, retain `master` and record the exact limitation instead of claiming parity.
4. Prefer small commits; scope expansion gets a new Issue. One PR and work branch per numbered implementation Issue. No direct push to `dev` or production. A PR must include `Refs #<issue>` and substantive Result, Implementation, Verification, Risk and Remaining work.
5. On failures, read causal job logs, fix one cause and rerun; at three failed attempts for the same cause, report the blocker. Keep evidence from previous runs identified as historical, not current success.

## Quality and exit criteria

- Command-line interface remains `./scripts/bootstrap --ci` plus `./ci/run fast|pr|release|nightly|audit`. Any cross-platform enhancement must be demonstrably runnable, not merely documented.
- No source code, credentials or runtime configuration for Claude/Anthropic (or any alternate coding agent) survives in active or distributable assets after slice 7; historical Git history/Issue descriptions are not rewritten.
- [ ] Every roadmap Issue is closed with verified implementation evidence.
- [ ] No roadmap implementation PR remains open. Existing unrelated Dependabot PRs are triaged but are not evidence of plan completion.
- [ ] All protected integration and production checks run successfully for the relevant final changes.
- [ ] Control Issue [#1](https://github.com/mlookhere/CI-Pipline/issues/1) reports actual production/integration heads, active work and release status.
- [ ] README and user-facing adoption instructions describe ChatGPT-only, without legacy installation requirements.

## Why this increases autonomy safely

State restoration is deterministic (GitHub, not conversation memory), actions are idempotent (retry cannot create duplicate work), CI failures have causal evidence (not a guessed fix), merge criteria are proven against branch protection, and consumers can synchronize upgrades without overwriting local application logic. Autonomy is deliberately bounded by tool permissions and required Actions gates.
