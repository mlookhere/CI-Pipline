# ChatGPT CI consumer synchronization — Item #101

`./scripts/pipeline-sync --check --upstream /path/to/CI-Pipline --consumer /path/to/project` reports actionable drift. Use `--dry-run` to preview an installation and `--install` to copy safe files; neither command downloads or executes an AI agent. The upstream is an existing trusted checkout of **mlookhere/CI-Pipline**.

## Explicit ownership

Owned: `ci/`, `scripts/`, `workflow/`, `.chatfreept/ci.json`, `.pre-commit-config.yaml`, `.gitattributes`, and the reviewed workflows `ci-pr.yml`, `ci-release.yml`, `security.yml`, `nightly-quality.yml`. Source and destination `.gitignore` rules are additively merged; existing consumer ignores are preserved. Unowned consumer files and secrets remain untouched.

Excluded: `.claude/`, `.claude-workflow.json`, Claude-named scripts, prompts, hooks, settings, skills, the advisory workflow, and consumer project metadata. Existing consumer-owned extras do not cause a failing check. `ci/mypy-advisory.ini` and repository setup scripts are consumer-owned or awaiting migration. This deliberately *does not* claim the still-legacy bootstrapping can operate in consumers without Item #102.

## Conflict handling

An absent upstream-owned file is `missing`; a recorded unmodified previous upstream copy is `stale` when a new upstream version exists. Only these may be copied during `--install`. A consumer modification without a known prior digest is a `conflict` that blocks the entire write; the user must reconcile it. An intentionally divergent consumer file can contain `# pipeline-sync: intentional`, which reports `intentional` but does not count as drift or get overwritten. Treat this marker as an explicit declaration of consumer ownership, not as a bypass for security checks. The local `.chatfreept/vendor-lock.json` remembers source content hashes, never credentials.

`--check` and `--dry-run` are read-only and use the same exit status: 0 for no actionable drift, 1 for missing/stale/conflict, 2 for malformed source/manifest. An install refuses symlink targets and conflicts; never force-overwrite. Run `--check` again after applying changes. Branch protection, workflow gate names and consumer test commands remain subject to separate validation.

Migration note: the current shared CI repository retains several Claude-named scripts and older runtime references until the independently gated #102 cutover. The sync plane intentionally excludes those files rather than copying a second autonomous agent into consumers. Validating a consumer's full headless CI after #102 remains required before a release claim. Existing consumer divergence bug [#59](https://github.com/mlookhere/CI-Pipline/issues/59) is addressed by explicit intentional status and non-failing consumer extras.
