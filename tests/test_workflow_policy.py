"""Control-plane policy tests.

The Dependabot target-branch rule is the one that bit us: without it Dependabot
opens pull requests against the default branch, which is the production branch,
so ci-pr.yml never fires and dependency changes reach production without
passing through integration.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from check_workflow_policy import (  # noqa: E402
    check_claude_job_text,
    check_claude_publisher_gate_text,
    check_closure_concurrency_text,
    check_dependabot,
    check_dependabot_text,
    check_privileged_inline_script_text,
    concurrency_block,
    group_of,
    integration_branch,
    job_blocks,
    job_if,
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


# The publisher is the privileged half of the advisory review: `pull-requests: write`
# and a comment on the pull request, reachable only through the Claude job's success.
# Three independent properties have to hold -- when it runs, whether a cancellation stops
# it, and what builds the body it posts -- and a rule phrased as "inspect your dependency
# properly" pins none of them: a condition that stops inspecting the dependency leaves the
# write-scoped job wide open while looking tidier than the one it replaced.
PUBLISHER = """name: Optional Claude advisory review

on:
  pull_request:
    types: [labeled, synchronize]

permissions: {}

jobs:
  review:
    if: >-
      contains(github.event.pull_request.labels.*.name, 'claude:review')
      && github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    outputs:
      review_json: ${{ steps.claude.outputs.structured_output }}
    steps:
      - id: claude
        uses: anthropics/claude-code-action@v1
  publish:
    needs: review
    if: ${{ !cancelled() && needs.review.result == 'success' && needs.review.outputs.review_json != '' }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - name: Validate and render review
        env:
          REVIEW_JSON: ${{ needs.review.outputs.review_json }}
        run: >-
          python3 workflow/render_claude_review.py
          --artifact /tmp/claude-review.json --comment /tmp/comment.md
      - run: gh pr comment "$PR_NUMBER" --body-file /tmp/comment.md
"""

COMMENT_STEP = '      - run: gh pr comment "$PR_NUMBER" --body-file /tmp/comment.md'

PUBLISHER_CONDITION = (
    "    if: ${{ !cancelled() && needs.review.result == 'success' "
    "&& needs.review.outputs.review_json != '' }}\n"
)


def _publisher(condition: str) -> str:
    text = PUBLISHER.replace(PUBLISHER_CONDITION, condition)
    assert text != PUBLISHER, "fixture replacement did not apply"
    return text


def test_the_publisher_must_gate_on_the_claude_jobs_success():
    assert check_claude_publisher_gate_text(PUBLISHER, "claude-review.yml") == []


def test_a_folded_condition_is_read_whole():
    """Splitting a condition over lines must not hide half of it from the reader."""
    condition = "    if: >-\n      ${{ !cancelled()\n      && needs.review.result == 'success' }}\n"
    body = dict(job_blocks(_publisher(condition)))["publish"]
    assert job_if(body) == ">- ${{ !cancelled() && needs.review.result == 'success' }}"


def test_a_steps_own_condition_is_not_read_as_the_jobs():
    """A step's `if:` sits deeper; reading it as the job's would wave the defect through."""
    text = _publisher("").replace(
        COMMENT_STEP,
        "      - if: ${{ !cancelled() && needs.review.result == 'success' }}\n"
        '        run: gh pr comment "$PR_NUMBER" --body-file /tmp/comment.md',
    )
    assert job_if(dict(job_blocks(text))["publish"]) is None


@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("    if: ${{ always() }}\n", id="always-alone"),
        pytest.param("    if: ${{ success() || failure() }}\n", id="success-or-failure"),
        pytest.param(
            "    if: ${{ always() && needs.review.outputs.review_json != '' }}\n",
            id="output-without-success",
        ),
        pytest.param("", id="no-condition-at-all"),
    ],
)
def test_a_write_scoped_publisher_that_stops_requiring_success_is_rejected(condition):
    failures = check_claude_publisher_gate_text(_publisher(condition), "claude-review.yml")
    assert any("gate on the advisory job's success" in failure for failure in failures)
    assert all("'publish'" in failure for failure in failures)


def test_always_is_rejected_on_a_write_scoped_publisher():
    """GitHub documents !cancelled() as the alternative *because* always() outlives a
    cancellation; on a job that comments as the repository that is worse than #31."""
    condition = "    if: ${{ always() && needs.review.result == 'success' }}\n"
    failures = check_claude_publisher_gate_text(_publisher(condition), "claude-review.yml")
    assert len(failures) == 1
    assert "keeps running after the run is cancelled" in failures[0]


def test_dropping_the_status_function_reintroduces_the_cancelled_report():
    """The regression this Issue exists for: the exact condition that shipped."""
    condition = "    if: needs.review.result == 'success' && needs.review.outputs.review_json != ''\n"
    failures = check_claude_publisher_gate_text(_publisher(condition), "claude-review.yml")
    assert len(failures) == 1
    assert "reports it cancelled rather than skipped" in failures[0]


def test_a_status_function_in_a_comment_does_not_count():
    """The condition GitHub evaluates does not include the comment beside it."""
    condition = "    if: needs.review.result == 'success'  # relies on !cancelled() elsewhere\n"
    failures = check_claude_publisher_gate_text(_publisher(condition), "claude-review.yml")
    assert len(failures) == 1
    assert "add !cancelled()" in failures[0]


def test_a_publisher_with_no_write_permission_is_not_constrained():
    """The rule is about privilege, not about every job downstream of the review."""
    text = PUBLISHER.replace("      pull-requests: write", "      pull-requests: read").replace(
        PUBLISHER_CONDITION, "    if: ${{ always() }}\n"
    )
    assert check_claude_publisher_gate_text(text, "claude-review.yml") == []


def test_a_double_quoted_success_comparison_is_accepted():
    text = PUBLISHER.replace("needs.review.result == 'success'", 'needs.review.result == "success"')
    assert check_claude_publisher_gate_text(text, "claude-review.yml") == []


def test_naming_a_different_job_does_not_satisfy_the_gate():
    """Gating on some other job's success leaves the Claude job's outcome unchecked."""
    text = PUBLISHER.replace("needs.review.result == 'success'", "needs.build.result == 'success'")
    assert len(check_claude_publisher_gate_text(text, "claude-review.yml")) == 1


def test_a_workflow_without_the_claude_action_is_not_constrained():
    text = PUBLISHER.replace("        uses: anthropics/claude-code-action@v1", "        run: echo hi")
    assert check_claude_publisher_gate_text(text, "ci-pr.yml") == []


def test_a_flow_mapping_job_fails_closed():
    """Valid YAML this line reader cannot see into must fail, not silently pass.

    Written as a flow mapping, `publish` is absorbed into the job above it, so the
    write permission and the condition both leave the reader's view.
    """
    # Sliced at the job heading rather than replacing the block verbatim: the steps this
    # publisher runs are load-bearing elsewhere in this file and will keep changing, and a
    # stale copy of them here would turn this test into a silent no-op.
    text = PUBLISHER[: PUBLISHER.index("  publish:")] + (
        '  publish: {needs: review, if: "${{ always() }}", runs-on: ubuntu-latest, '
        "permissions: {pull-requests: write}, steps: [{run: echo publish}]}\n"
    )
    assert text != PUBLISHER, "fixture replacement did not apply"
    failures = check_claude_publisher_gate_text(text, "claude-review.yml")
    assert any("flow mapping" in failure for failure in failures)


def test_job_names_at_the_wrong_indentation_fail_closed():
    """Re-indenting the jobs hides every job from the reader; that is a failure."""
    text = "\n".join(
        f"  {line}" if line.startswith(("  review:", "  publish:")) else line
        for line in PUBLISHER.splitlines()
    )
    failures = check_claude_publisher_gate_text(text, "claude-review.yml")
    assert any("no job block containing it could be located" in failure for failure in failures)


def test_the_real_claude_review_publisher_is_gated():
    text = (WORKFLOWS / "claude-review.yml").read_text(encoding="utf-8")
    assert "pull-requests: write" in text, "test is pinned to the wrong file"
    assert check_claude_publisher_gate_text(text, "claude-review.yml") == []


def test_every_workflow_in_the_repository_satisfies_the_publisher_rule():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        assert check_claude_publisher_gate_text(text, path.name) == [], path.name


# Issue #41: the publisher used to render the comment from an unvalidated heredoc. Two
# rules replace it, and they fail for different reasons -- one that the body is built by
# the reviewed renderer, one that no privileged job runs code no gate can read. Either
# alone leaves a way back: a heredoc that also mentions the renderer, or a call to some
# other script that never validates anything.
INLINE_RENDER = (
    "      - name: Validate and render review\n"
    "        run: |\n"
    "          python3 - <<'PY2'\n"
    "          import json, os\n"
    "          print(json.loads(os.environ['REVIEW_JSON'])['summary'])\n"
    "          PY2\n"
)


def test_a_publisher_that_stops_using_the_renderer_is_rejected():
    text = PUBLISHER.replace("python3 workflow/render_claude_review.py\n", "python3 workflow/other.py\n")
    assert text != PUBLISHER, "fixture replacement did not apply"
    failures = check_claude_publisher_gate_text(text, "claude-review.yml")
    assert len(failures) == 1
    assert "render_claude_review.py" in failures[0]
    assert "'publish'" in failures[0]


def test_a_job_that_posts_no_comment_is_not_asked_to_render_one():
    """The rule is about the comment body, not about every write-scoped job downstream."""
    text = PUBLISHER.replace(COMMENT_STEP, "      - run: gh pr edit --add-label reviewed")
    assert text != PUBLISHER, "fixture replacement did not apply"
    assert check_claude_publisher_gate_text(text, "claude-review.yml") == []


def test_a_heredoc_in_a_write_scoped_job_is_rejected():
    """The exact shape this Issue removed, including one that still names the renderer."""
    text = PUBLISHER.replace(COMMENT_STEP, INLINE_RENDER + COMMENT_STEP)
    assert text != PUBLISHER, "fixture replacement did not apply"
    failures = check_privileged_inline_script_text(text, "claude-review.yml")
    assert len(failures) == 1
    assert "'publish'" in failures[0]
    assert "interpreter on stdin" in failures[0]


def test_a_read_only_job_may_still_build_a_script_inline():
    """Privilege is what makes unreadable code dangerous; a read-only job is not the target."""
    text = PUBLISHER.replace("      pull-requests: write", "      pull-requests: read").replace(
        COMMENT_STEP, INLINE_RENDER + COMMENT_STEP
    )
    assert check_privileged_inline_script_text(text, "claude-review.yml") == []


def test_a_shell_flag_is_not_mistaken_for_a_stdin_program():
    """`bash -c` and `--` are ordinary arguments; only a lone `-` reads a program."""
    text = PUBLISHER.replace(COMMENT_STEP, '      - run: bash -c "gh pr comment -- --body-file x"')
    assert check_privileged_inline_script_text(text, "claude-review.yml") == []


def test_the_real_claude_review_publisher_renders_through_the_script():
    text = (WORKFLOWS / "claude-review.yml").read_text(encoding="utf-8")
    assert "pull-requests: write" in text, "test is pinned to the wrong file"
    assert "workflow/render_claude_review.py" in text
    assert check_privileged_inline_script_text(text, "claude-review.yml") == []


def test_no_workflow_runs_privileged_code_a_gate_cannot_read():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        assert check_privileged_inline_script_text(text, path.name) == [], path.name


# Acceptance criterion 3 of Issue #31: the security posture of this workflow must not
# move. Those four properties were asserted only against the one real file by a CI run;
# extracting them made each reachable from a test that can actually try to break it.
@pytest.mark.parametrize(
    ("mutation", "replacement", "expected"),
    [
        pytest.param(
            "      pull-requests: read",
            "      pull-requests: write",
            "repository write permission",
            id="claude-key-job-granted-write",
        ),
        pytest.param(
            '--disallowedTools "Edit,Write,NotebookEdit"',
            "--allowedTools Read",
            "deny edit tools",
            id="edit-tools-no-longer-denied",
        ),
        pytest.param(
            "sanitize_claude_input.py",
            "cat_the_diff.py",
            "bounded sanitizer",
            id="prompt-input-not-sanitized",
        ),
        pytest.param(
            "head.repo.full_name == github.repository",
            "true",
            "reject forked pull requests",
            id="forks-no-longer-rejected",
        ),
    ],
)
def test_the_claude_job_security_properties_are_each_enforced(mutation, replacement, expected):
    text = (WORKFLOWS / "claude-review.yml").read_text(encoding="utf-8")
    assert check_claude_job_text(text, "claude-review.yml") == [], "the real file must be clean"
    broken = text.replace(mutation, replacement)
    assert broken != text, "mutation did not apply; the test is pinned to stale text"
    failures = check_claude_job_text(broken, "claude-review.yml")
    assert any(expected in failure for failure in failures), failures


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
