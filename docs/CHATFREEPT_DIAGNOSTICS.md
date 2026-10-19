# CI failure diagnostics and bounded retry policy — Issue #100

Use `workflow/chatfreept_diagnostics.py --input <fresh-observation.json>` after a GitHub Actions run reports an unsuccessful job, or when the run is still pending. The JSON must be assembled from **fresh, authenticated GitHub MCP observations**, not guessed from a chat summary.

Provide exact `head_sha`, the GitHub `run` (id, head_sha, status, conclusion, html_url), fully paginated `jobs` (total_count and jobs), and a `logs` dictionary keyed by failed job ID. Supply a previous `attempts` map from the owning Issue if available. The module makes no network call and no GitHub mutation.

The output describes only causal diagnostics. `ci_verified` is **always false**; required-check reconciliation and GitHub branch enforcement remain separate. `pending` does not mean passed. Missing logs and incomplete pagination return non-success and do not guess a fix. Optional skipped jobs are not failures, but this diagnostic tool never certifies a merge.

The first failed job is selected deterministically by job ID and its earliest failed step. Log excerpts are redacted for common tokens, passwords and Authorization headers, limited to the last 55 lines and 4,096 characters. Fingerprints use a digest of the job, step and redacted error line so a repeated cause can be tracked without uploading raw logs to Issues.

**Retry contract:** At most three unsuccessful fixes for one fingerprint. The invoking ChatGPT session increments `failed_fixes` only when `fix_applied: true` and a different head SHA is observed. Re-reading the same head cannot consume another attempt. After three unsuccessful fixes, report a blocker and request human input rather than weakening a test or threshold. A possible transient infrastructure cause is merely classified, not automatically rerun or waived.

When checks are running, return resumable pending state; future ChatGPT continuations re-fetch GitHub. Never run an alternate autonomous agent or a credential-bearing CI worker.
