---
name: pr-readiness
description: Prepare a task branch for PR without weakening gates. Use when implementation is believed complete or CI is about to run.
---

# PR readiness

1. Re-read the controlling Issue acceptance criteria.
2. Inspect the complete diff and classify changed paths by risk.
3. Run targeted tests, then `./ci/run fast`, then `./ci/run pr` when feasible.
4. Confirm no secrets, focused/skipped tests, debug code, unrelated files, generated-file drift, or threshold suppressions.
5. Update the Issue handoff.
6. Open/update the PR with `./flow pr ISSUE`. CI remains authoritative.
