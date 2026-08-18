# CI-Pipline

An Issue/PR/CI control plane for Claude Code, extracted from a working repository rather
than written as a template. Every guard in it exists because something got through.

It gives a repository one shape of work — **one Issue, one branch, one pull request** — and
enforces that shape with local hooks, deterministic gates and GitHub workflows, so that the
state of the work is readable from the repository instead of from a conversation.

## What is in it

| Part | What it does |
|---|---|
| `CLAUDE.md` | the operating contract a Claude Code session reads first |
| `.claude/settings.json`, `.claude/hooks/` | lifecycle hooks: session context, a Bash command-policy gate, post-edit validation, and a completion-evidence check at stop |
| `.claude/rules/` | the written command and release policy the hooks enforce |
| `.claude-workflow.json` | branches, command groups, stages, risk paths, quality budgets — every local and hosted gate routes through this one file |
| `ci/run` | the deterministic stage runner, writing machine-readable evidence to `artifacts/ci/` |
| `ci/quality.py` | changed-file quality gate: secret patterns, detect-secrets, debug statements, unreferenced work markers, size and complexity budgets |
| `flow` | the Issue → branch → PR → release commands |
| `workflow/` | control-plane self-test, PR metadata validation, workflow policy checks, Action pinning, release manifests |
| `.github/workflows/` | PR gate, release gate, supply-chain checks, nightly quality, control-Issue sync, optional advisory Claude review |
| `.pre-commit-config.yaml` | staged-file gates on commit, the fast gate on push |

## The daily loop

```bash
./scripts/bootstrap                  # once per clone: CI toolchain and git hooks
./flow new --title "..."             # Issue, branch and worktree in one step
# ...edit, run targeted tests...
./ci/run fast && ./ci/run pr         # evidence before the pull request
./flow pr <issue>
```

`./ci/run <stage>` is the only entry point any gate uses. What a stage runs is configuration,
never code, so adapting the plane to a repository means editing `.claude-workflow.json` and
nothing else.

## The rules that are actually enforced

Worth stating separately, because each of these was once a check that reported success
having verified nothing:

- **A stage may not name a command group that runs nothing.** `ci/run.py` skips an empty
  group in silence, so a name written into a stage as a statement of intent reported success
  forever. `self_test.check_stage_commands` now fails the gate instead. This was found when a
  release gate naming eleven checks turned out to run four.
- **Every gating job must be a required check.** A job that runs but does not gate is a check
  that reports rather than blocks; a required check whose job was deleted blocks every merge
  instead. Both are silent until a pull request is open, so a test asserts the two lists
  against each other.
- **A hook that crashes must refuse, not allow.** The policy hook prints its denial before it
  writes its audit log, treats any non-zero exit from a checker as a failure, and exits with
  the blocking status when it cannot evaluate a command at all.
- **A push to an integration or production branch is parsed, not pattern-matched.**
  `git push origin +dev` and `git push origin HEAD:refs/heads/dev` are the same operation as
  `git push origin dev`, and a substring check let both through.
- **No file that ships with the plane may name the product it ships with.** The package and
  witness names come from `.claude-workflow.json`; `self_test.check_no_product_names` fails
  the gate if a portable file hard-codes one.

## Adopting it

1. Copy `.claude/`, `ci/`, `workflow/`, `scripts/`, `flow`, `.claude-workflow.json` and
   `.github/` into the repository. The Claude Code hooks have to live in the consumer's own
   `.claude/`, so vendoring is the distribution model rather than a shortcut around one.
2. Edit `.claude-workflow.json` only:
   - `branches`, `github.expected_owner`, `github.expected_repository`
   - `github.risk_paths` — the globs that force a risk label, per repository
   - `project` — `package_dir`, `packaged_assets`, `typed_advisory_witness`, `executables`,
     `forbidden_call_sites`. All optional; every check that reads one is inert when it is
     empty.
   - `commands` and `stages` — switch on what the repository can actually run. A repository
     with a package adds `build`, `dependency_sync`, `clean_install`, `package_release` and
     `sbom`; this one has none of those and names none of them.
3. `./scripts/bootstrap`, then `./scripts/setup-github` to install labels, the pinned control
   Issue and branch protection.
4. `./scripts/pipeline-sync --check --upstream <path-or-url>` reports where the vendored copy
   has drifted from this repository. Configuration and tests are excluded from that
   comparison, because those are the consumer's.

## What this repository does not have

No package, no `requirements.txt`, no Dockerfile, no deployment. `pyproject.toml` here holds
tool configuration and nothing else — deliberately no `[project]` and no `[build-system]`, so
the plane stays something a repository vendors rather than something that has to be released
before it can be used.

## Repository setup that a person has to do

Seeding `dev` and `master` on a new remote is refused by the same push guard this plane
installs, which is correct: it cannot tell a first push from a direct push to production.
Create the branches by hand once, then run `./scripts/setup-github`.

```bash
git push origin HEAD:refs/heads/master
git push origin HEAD:refs/heads/dev
gh api -X PATCH repos/{owner}/{repo} -f default_branch=dev
./scripts/setup-github
```

`scripts/setup-github` also needs admin rights on the repository: it sets the
`ENFORCE_PINNED_ACTIONS` variable and installs branch protection. Branch protection requires
a public repository or a paid plan — on a free private repository the API returns 403 and the
required-checks list is declarative only, enforced by the local hooks and by discipline.
