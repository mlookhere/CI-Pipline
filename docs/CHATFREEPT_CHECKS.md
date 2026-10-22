# GitHub required-check evidence reconciliation — Issue #99

The authoritative inputs are fresh responses from GitHub for the **same repository, base branch, event, and exact work/queue SHA**. The static manifest lists expected checks; it is *not proof* that GitHub branch protection or a job has passed.

## Read and verify

1. Read the current PR head SHA (for a `pull_request`) or merge queue SHA (for `merge_group`); re-read the branch ref immediately before a merge. Pull-request and merge-group evidence **must not be interchanged**.
2. Read live protection for the base branch via GitHub's required-status-checks endpoint. Supply the returned `contexts` and `checks[].context` (do not replace a failed fetch with local configuration). Missing/403 protection evidence is **not mergeable**.
3. Retrieve *all pages* of GitHub Actions workflow runs and jobs for that exact SHA, including current status and conclusion. Record `total_count` accurately; incomplete paging blocks approval.
4. Create an evidence JSON file shaped as the inputs to `tests/test_chatfreept_checks.py`: `repository`, `base_branch`, `event`, `head_sha`, `event_head_sha`, `observed_head_sha`, `protection`, `workflow_runs`, `jobs`. Add each job's source workflow `run_id` and `head_sha` from the GitHub API. Do not fabricate fields or copy a prior run's results. Use `python workflow/chatfreept_checks.py --evidence <path>`.
5. If any check is missing, pending, red, cancelled, stale, duplicated, or skipped-required, **do not merge**. Only explicitly optional named jobs can be skipped; required checks cannot. Re-observe and rerun after changes.

The program is an **offline, deterministic validator**, not a token-bearing background worker. It cannot fetch GitHub data itself and does not automatically merge, issue permissions, or mark a PR approved. A client must verify live source authenticity and recheck the head before acting. It is deliberately stricter about partial pagination and ambiguous duplicate jobs. GitHub remains the final protected merge authority.

**Current access limitation:** the installed GitHub App connection returned 403 for both `dev` and `master` required-status-checks-protection GET endpoints. Therefore this program cannot presently validate *actual* branch protection with this connection alone. A future administrator-authorized read or equivalent trustworthy source must supply that specific evidence; do not substitute the local manifest or assert success in its absence.

For integration, the checked-in expected set is PR metadata, Fast deterministic gates, PR test/build gates, Dependency audit, and Workflow policy. For protected production (currently `master`), it is Release metadata, Full regression and production build, and Workflow policy. These names are expectations, not proof of active protection.
