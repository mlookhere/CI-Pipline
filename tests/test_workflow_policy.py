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
    concurrency_block,
    group_of,
    integration_branch,
    job_blocks,
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


def test_sharing_one_group_name_is_rejected_however_the_closure_job_is_configured():
    """cancel-in-progress belongs to the *incoming* run, so the sync would still win.

    This is the shape a well-meaning refactor lands on: two jobs, each with its own
    `concurrency:` block, both naming the same group. It reproduces Issue #36 exactly
    while looking like the fix for it.
    """
    text = SPLIT_GROUPS.replace(
        "      group: control-issue-close-${{ github.event.pull_request.number }}",
        "      group: control-issue",
    ).replace("      group: control-issue-sync", "      group: control-issue")
    failures = check_closure_concurrency_text(text, "sync-control.yml")
    assert len(failures) == 1
    assert "'close'" in failures[0] and "'sync'" in failures[0]
    assert "would cancel this one" in failures[0]


def test_a_quoted_true_does_not_slip_past():
    text = SPLIT_GROUPS.replace("cancel-in-progress: false", "cancel-in-progress: 'true'")
    assert len(check_closure_concurrency_text(text, "sync-control.yml")) == 1


def test_an_expression_is_read_as_cancelling_rather_than_waved_through():
    text = SPLIT_GROUPS.replace(
        "cancel-in-progress: false",
        "cancel-in-progress: ${{ github.event_name != 'pull_request_target' }}",
    )
    assert len(check_closure_concurrency_text(text, "sync-control.yml")) == 1


def test_an_inline_mapping_is_read_like_a_block():
    text = SPLIT_GROUPS.replace(
        "    concurrency:\n"
        "      group: control-issue-close-${{ github.event.pull_request.number }}\n"
        "      cancel-in-progress: false",
        "    concurrency: {group: control-issue-close, cancel-in-progress: true}",
    )
    assert len(check_closure_concurrency_text(text, "sync-control.yml")) == 1


def test_an_earlier_mention_of_the_handler_does_not_hide_a_later_job():
    """The guard reads every job that names the handler, not the first occurrence."""
    text = SPLIT_GROUPS.replace(
        "  close:\n",
        "  decoy:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo not handle_pr_state.py here\n"
        "  close:\n",
    ).replace("cancel-in-progress: false", "cancel-in-progress: true")
    failures = check_closure_concurrency_text(text, "sync-control.yml")
    assert any("'close'" in failure for failure in failures)


def test_a_group_expression_keeps_its_closing_braces():
    """A naive `[^}]+` group regex truncates `${{ ... }}` and breaks the sharing check."""
    assert group_of(concurrency_block(SPLIT_GROUPS[SPLIT_GROUPS.index("  close:") :])) == (
        "control-issue-close-${{ github.event.pull_request.number }}"
    )


def test_on_subkeys_are_not_mistaken_for_jobs():
    names = [name for name, _ in job_blocks(SPLIT_GROUPS)]
    assert names == ["close", "sync"], names


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


def test_the_real_sync_job_still_runs_after_the_closure_job():
    """Splitting one job into two lost an ordering that used to be free.

    `sync_control` renders the control block from every open Issue's state labels, so
    running it concurrently with the handler that moves this pull request's Issue can
    publish a body listing shipped work as still active.
    """
    text = (WORKFLOWS / "sync-control.yml").read_text(encoding="utf-8")
    blocks = dict(job_blocks(text))
    assert set(blocks) == {"close", "sync"}, sorted(blocks)
    assert "needs: close" in blocks["sync"]
    assert "!cancelled()" in blocks["sync"], "sync must still run when close is skipped or fails"
