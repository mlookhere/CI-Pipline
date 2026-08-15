"""Control-plane policy tests.

The Dependabot target-branch rule is the one that bit us: without it Dependabot
opens pull requests against the default branch, which is the production branch,
so ci-pr.yml never fires and dependency changes reach production without
passing through integration.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from check_workflow_policy import (  # noqa: E402
    check_closure_concurrency_text,
    check_dependabot,
    check_dependabot_text,
    integration_branch,
)

WORKFLOWS = ROOT / ".github" / "workflows"

ECOSYSTEM = """version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
{target}    schedule:
      interval: "weekly"
"""


def _config(target: str = "") -> str:
    return ECOSYSTEM.format(target=target)


def test_missing_target_branch_is_rejected():
    failures = check_dependabot_text(_config(), "dev")
    assert len(failures) == 1
    assert "sets no target-branch" in failures[0]
    assert "'pip'" in failures[0]


def test_production_target_branch_is_rejected():
    failures = check_dependabot_text(_config('    target-branch: "master"\n'), "dev")
    assert len(failures) == 1
    assert "targets 'master'" in failures[0]
    assert "expected the integration branch 'dev'" in failures[0]


def test_integration_target_branch_passes():
    assert check_dependabot_text(_config('    target-branch: "dev"\n'), "dev") == []


def test_unquoted_target_branch_passes():
    assert check_dependabot_text(_config("    target-branch: dev\n"), "dev") == []


def test_every_ecosystem_is_checked_independently():
    text = """version: 2
updates:
  - package-ecosystem: "pip"
    target-branch: "dev"
    directory: "/"
  - package-ecosystem: "docker"
    directory: "/"
"""
    failures = check_dependabot_text(text, "dev")
    assert len(failures) == 1
    assert "'docker'" in failures[0]


def test_no_ecosystems_is_not_a_failure():
    assert check_dependabot_text("version: 2\nupdates: []\n", "dev") == []


def test_this_repository_satisfies_the_rule():
    """Regression guard: the real dependabot.yml must stay compliant."""
    assert check_dependabot() == []


def test_integration_branch_comes_from_the_workflow_config():
    assert integration_branch() == "dev"


# The shape that actually shipped, and that dropped two consecutive merge transitions:
# one workflow-level cancelling group covering the one-shot handler and the periodic sync.
SHARED_GROUP = """name: Sync control issue

on:
  pull_request_target:
    types: [closed]
  workflow_run:
    workflows: ["PR CI"]
    types: [completed]

concurrency:
  group: control-issue-sync
  cancel-in-progress: true

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - name: Update controlling Issue after PR closure
        if: github.event.action == 'closed'
        run: python3 workflow/handle_pr_state.py
      - name: Synchronize the pinned control Issue
        run: python3 workflow/claude_flow.py sync-control
"""

SPLIT_GROUPS = """name: Sync control issue

on:
  pull_request_target:
    types: [closed]
  workflow_run:
    workflows: ["PR CI"]
    types: [completed]

jobs:
  close:
    if: github.event.action == 'closed'
    runs-on: ubuntu-latest
    concurrency:
      group: control-issue-close-${{ github.event.pull_request.number }}
      cancel-in-progress: false
    steps:
      - run: python3 workflow/handle_pr_state.py
  sync:
    runs-on: ubuntu-latest
    concurrency:
      group: control-issue-sync
      cancel-in-progress: true
    steps:
      - run: python3 workflow/claude_flow.py sync-control
"""


def test_a_workflow_level_cancelling_group_over_the_closure_handler_is_rejected():
    failures = check_closure_concurrency_text(SHARED_GROUP, "sync-control.yml")
    assert len(failures) == 1
    assert "covers every trigger" in failures[0]
    assert "sync-control.yml:10:" in failures[0]


def test_splitting_the_groups_passes():
    assert check_closure_concurrency_text(SPLIT_GROUPS, "sync-control.yml") == []


def test_the_idempotent_sync_may_still_cancel_itself():
    """The whole point of the split: `sync` keeps cancel-in-progress, `close` does not."""
    assert "cancel-in-progress: true" in SPLIT_GROUPS
    assert check_closure_concurrency_text(SPLIT_GROUPS, "sync-control.yml") == []


def test_a_cancelling_group_on_the_closure_job_itself_is_rejected():
    text = SPLIT_GROUPS.replace(
        "      group: control-issue-close-${{ github.event.pull_request.number }}\n"
        "      cancel-in-progress: false",
        "      group: control-issue-close\n      cancel-in-progress: true",
    )
    failures = check_closure_concurrency_text(text, "sync-control.yml")
    assert len(failures) == 1
    assert "must not be cancellable" in failures[0]


def test_a_workflow_without_the_closure_handler_is_not_constrained():
    """Every other workflow keys a cancelling group on the ref; none of them may trip this."""
    text = SHARED_GROUP.replace("python3 workflow/handle_pr_state.py", "echo nothing-to-do")
    assert check_closure_concurrency_text(text, "ci-pr.yml") == []


def test_the_real_sync_control_workflow_satisfies_the_rule():
    """Regression guard against the defect returning to the file it was found in."""
    text = (WORKFLOWS / "sync-control.yml").read_text(encoding="utf-8")
    assert "handle_pr_state.py" in text, "test is pinned to the wrong file"
    assert check_closure_concurrency_text(text, "sync-control.yml") == []
