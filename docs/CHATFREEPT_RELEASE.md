# Production release protocol — ChatGPT / GitHub MCP

## Release ownership

The only autonomous engineering agent is ChatGPT through authorized GitHub tools. GitHub
Actions runs deterministic, headless gates; the Chrome Chat FreePT extension only drives
the next ChatGPT turn. This repository currently uses **dev** for integration and protected
**master** for production. Never infer the production branch from the Chrome extension's
generic prompts, rename branches, change the default, or force-push.

A production release gets a separate open **type:release** Issue and a PR from **dev**
into **master**. A work-branch PR to dev is not a production release. The actual
release PR must pass all three production checks: **Release metadata**,
**Full regression and production build**, **Workflow policy**.

## Deterministic proposal, no invisible writes

Use the existing release Issue/PR when present. Before planning, fetch the current
repository, dev and production exact heads, all open PRs (fully paginated), a release Issue
and its labels, and the precise changed paths, changed-file count and changed-line count
for dev versus production. Never invent diff metrics or treat missing API fields as empty.

Pass those observations to:

    python workflow/release_plan.py --observation /path/to/fresh-observation.json

The input is ephemeral, not part of the repository. Its schema contains:
- repository: current owner/name (the selected repository, never a copied template name)
- branches: mapping of actual integration and production branch names to their 40-hex SHAs
- expected_source_sha / expected_target_sha: freshly observed exact SHA preconditions
- issue: existing number, open state, list of existing labels including type:release
- diff: changed_files, changed_lines, and the complete list of changed paths
- open_pull_requests: complete list of objects with number, head branch, base branch and body
- open_pull_requests_total: the fully paginated GitHub count, exactly matching the list length

This **read-only** planner returns either propose_pr or resume_pr, the full required
Issue/PR risk-label sets, release body, rollback reference and expected SHAs. It does
not create Issues, branches, PRs, releases, tags or GitHub labels, and cannot approve a
merge. ChatGPT must re-read GitHub facts immediately before performing each proposed
MCP mutation and verify the readback. Stale SHA, zero diff, wrong Issue, missing
open-PR list, duplicate PR or wrong repository all stop the process.

The same max_pr_files and max_pr_changed_lines policy used by integration metadata
checks is used for production. If **either** budget is exceeded, apply
risk:large-change on **both the release Issue and PR** before expecting the release
metadata job to pass. Do not increase limits or suppress the check to accommodate an
aggregated release. Derive additional risk labels from changed paths. Previously
applied labels should be preserved, not duplicated. This closes the manual-label
gap tracked in Issue #60 once used during an actual release.

## Exact-head acceptance and recovery

The production PR requires Result, Implementation, Verification, Risk and Remaining
work sections, a reference to its open type:release Issue, the candidate source SHA,
baseline production SHA, diff summary, and a rollback procedure. Use actual release
metadata from the correct GitHub PR and current repository branch policy, not a
ChatGPT status message. All required checks must be completed and successful on the
exact PR head, with any required approvals and branch protection confirmed.

If GitHub branch-protection details cannot be read (for example, integration
returns HTTP 403), do **not** assert equivalent protection, green merge eligibility
or completion. Continue testing, documentation and PR preparation; only request
permissions if the external branch-protection validation is the remaining blocker.
A skipped, zero, missing, stale, pending or red required check is never success.

On successful production merge, verify the new production head and deployment health.
A rollback is a separate controlled revert/release PR into master, based on the known-good
pre-release production SHA. Never hard-reset or force-push the protected branch. Only
close the release Issue after verifiable production results. Finally inspect open
plan Issues, PRs, and the relevant Actions runs before claiming project Complete.

For a Chrome extension consumer, require reproducible source-owned MV3 build,
manifest and permission checks, content/service-worker integration tests, store
package verification, and actual browser smoke tests in the consumer source repository.
A compiled ZIP uploaded to ChatGPT is not evidence of source-level tests.
