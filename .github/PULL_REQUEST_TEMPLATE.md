## Issue

Closes #

## Result

Describe the observable change in behavior. What can a reviewer now do, or no
longer do, that was different before?

## Implementation

The important technical decisions and trade-offs, not a file-by-file
transcript. Call out anything a reviewer would not predict from the diff.

## Verification

- [ ] `./ci/run fast` passed locally on the final commit
- [ ] `./ci/run pr` passed locally on the final commit
- [ ] Regression coverage added or updated for behavior changes
- [ ] Required GitHub checks are green

Paste the exact commands run and their results. For a change to answering
behavior, state how grounding, citation, and refusal-when-unsourced were
verified.

## Risk

Database, security, deployment, dependency, compatibility, and rollback
implications. Risk labels applied per `risk_paths` in `.claude-workflow.json`:
yes/no. Write `Low — <concrete reason>` only with a concrete reason.

## Remaining work

Anything deliberately deferred, with Issue references. Write `None` when
complete.
