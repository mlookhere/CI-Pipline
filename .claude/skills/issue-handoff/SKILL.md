---
name: issue-handoff
description: Update the existing controlling GitHub Issue with concise current truth. Use when ending, pausing, compacting, or transferring work.
---

# Issue handoff

1. Inspect branch, HEAD, worktree, diff, PR, checks, and acceptance criteria.
2. Write only current facts under the repository's required managed headings.
3. Include exact verification commands and results; distinguish pass, fail, and not run.
4. Include current commit, PR, dirty state, blockers, risks, and next exact actions.
5. Run `./flow handoff ISSUE --state-file /tmp/issue-state.md`, then delete the temporary file.
6. Never create or commit a handoff document. Remove stale or contradictory text rather than appending history.
