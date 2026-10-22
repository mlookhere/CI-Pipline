# Chat FreePT GitHub handoffs (Issue #98)

## Durable source of truth

ChatGPT is the only autonomous engineering agent. At the beginning of each continuation read the live control Issue, owning Issue, branches, open/closed PRs and Actions checks through **the authorized GitHub MCP connector**. Never infer success from a prior chat status or reuse check conclusions from an earlier head SHA. The CLI below is a *deterministic offline planner* and does not contact GitHub, mutate Issues, or execute another agent.

The active production branch is protected \`master\`; integration/default is \`dev\`. A task uses one Issue, one \`work/<issue>-<slug>\` branch and one PR into \`dev\`. Issue #99 is responsible for reconciling actual protected required checks to per-SHA GitHub Actions conclusions. The handoff planner always reports \`ci: unverified\` and never grants merge permission.

## Restart sequence

1. Fetch GitHub repository identity, Issue number/open status/full body/updated_at, exact work-branch name and SHA, and all PRs relevant to the work branch. Missing API fields are errors, not negative answers.
2. Find existing PR by branch/base and \`Refs #<issue>\`. Multiple open PRs, wrong links and mismatched heads are conflicts; do **not** create a duplicate. If no PR exists, resume the work branch rather than creating another Issue/branch.
3. Read the \`<!-- chatfreept-handoff:start -->\` block from the Issue body. Preserve all other Issue text and managed markers. An invalid/multiple block is a blocker, never silently reset.
4. Prepare a JSON observation for \`workflow/chatfreept_state.py\` (run with \`python workflow/chatfreept_state.py --input observation.json\`). Supply the observed Issue timestamp, head SHA, previous revision, unique session actor ID, explicit epoch-second timestamp and lease TTL (default 1,800s; max 28,800s). The file is local ephemeral input, **never commit or upload credentials**. The planner emits the complete proposed Issue body and preconditions or rejects the operation.
5. **Immediately before mutation**, re-fetch Issue body/updated_at, work-branch SHA and matching PRs. Compare against the emitted preconditions, including revision. On mismatch, discard the plan and reconcile anew. Never try a blind overwrite.
6. Only after comparison, update the existing owning Issue through GitHub MCP, then re-fetch it to verify the full managed block survived. If an update conflicts or the readback differs, report the conflict and stop; do not erase foreign work. Keep original Issue content intact.
7. A live lease held by another actor stops mutation. An expired lease may be taken over after full fresh re-observation. Same session and unchanged state is an idempotent no-op.

### Concurrency limitation

GitHub Issue body updates do not expose an atomic compare-and-swap primitive through the current connector. The revision/timestamp/lease checks are **optimistic safety checks**, not a distributed lock or a transaction. A concurrent write between the last read and the update can still race. Require a fresh read and post-write verification; avoid simultaneous writers. Actual branch-ref changes must use exact expected-SHA updates, not force pushes. Never claim that a successful local validation permits a merge.

### Lifecycle and evidence

\`developing\`: work branch exists and no matching PR; \`review\`: exactly one matching open PR; \`merged\`: matching PR is closed and GitHub reports merged. This is *observational state*, not an instruction to merge or a claim of CI green. The Issue/PR and Actions are authoritative when the snapshot conflicts. Separate PR/release transitions require protected gate evidence and approvals as appropriate. Existing code may still run legacy Claude-configured validation until gated removal in #102, but the autonomous worker is ChatGPT only.

### Schema summary

The managed block contains schema version 1, strictly increasing revision, repository identity, Issue number, branch/head SHA, matching PR number if present, observed phase, \`ci: unverified\`, and lease holder/expiry epoch. Never copy secrets into this block.
