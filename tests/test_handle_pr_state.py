"""The one-shot handler that moves a controlling Issue when its pull request closes.

`check_closure_concurrency_text` exists to keep this uncancellable, because the event will
never be re-delivered: losing the run leaves an Issue claiming work is active after it shipped.
That is a guard built around code with no tests, which is the wrong way round.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import handle_pr_state  # noqa: E402


def pr(base: str, head: str = "work/7-something", body: str = "") -> dict:
    return {"base": {"ref": base}, "head": {"ref": head}, "body": body}


# ----------------------------------------------------------- which Issue a PR belongs to


@pytest.mark.parametrize(
    "head",
    [
        pytest.param("work/7-something", id="prefixed"),
        pytest.param("work/7", id="no-slug"),
        pytest.param("7-something", id="no-prefix"),
    ],
)
def test_a_task_pull_request_is_read_from_its_branch(head):
    assert handle_pr_state.issue_from_pr(pr("dev", head=head), "dev", "master") == 7


def test_a_release_pull_request_is_read_from_its_body():
    """`dev` is the head of a release, so the branch says nothing about which Issue owns it."""
    got = handle_pr_state.issue_from_pr(pr("master", head="dev", body="Refs #28\n"), "dev", "master")

    assert got == 28


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("Fixes #28", id="fixes"),
        pytest.param("closes #28", id="closes-lowercase"),
    ],
)
def test_the_other_link_verbs_are_accepted(body):
    assert handle_pr_state.issue_from_pr(pr("master", head="dev", body=body), "dev", "master") == 28


@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param(pr("dev", head="rename-the-thing"), id="branch-names-no-issue"),
        pytest.param(pr("master", head="dev", body="no link here"), id="body-names-no-issue"),
        pytest.param(pr("some-other-branch", head="work/7-x"), id="neither-base"),
    ],
)
def test_a_pull_request_with_no_owning_issue_is_left_alone(candidate):
    """Returning 0 or guessing would move an Issue that has nothing to do with the change."""
    assert handle_pr_state.issue_from_pr(candidate, "dev", "master") is None


def test_a_task_branch_body_is_not_consulted():
    """The branch is authoritative on the integration side; a stray `Refs #99` must not win."""
    got = handle_pr_state.issue_from_pr(pr("dev", head="work/7-x", body="Refs #99"), "dev", "master")

    assert got == 7


# ----------------------------------------------------------- what it does to the Issue


def _record(monkeypatch, state: str, labels: list[str]) -> list[tuple]:
    calls: list[tuple] = []

    def fake(*args: str, capture: bool = False) -> str:
        calls.append(args)
        if capture:
            import json

            return json.dumps({"state": state, "labels": [{"name": name} for name in labels]})
        return ""

    monkeypatch.setattr(handle_pr_state, "gh", fake)
    return calls


def test_the_previous_state_label_is_removed_and_the_new_one_added(monkeypatch):
    calls = _record(monkeypatch, "OPEN", ["state:review", "risk:ci"])

    handle_pr_state.set_state(7, "state:release-ready", "shipped")

    edit = next(c for c in calls if c[0] == "issue" and c[1] == "edit")
    assert "--remove-label" in edit and "state:review" in edit
    assert "--add-label" in edit and "state:release-ready" in edit
    assert "risk:ci" not in edit, "risk labels are not state and must survive the transition"


def test_a_closed_issue_is_not_touched(monkeypatch):
    """The handler answers an event, not a request: it must not reopen finished work."""
    calls = _record(monkeypatch, "CLOSED", ["state:review"])

    handle_pr_state.set_state(7, "state:release-ready", "shipped")

    assert not any(c[1] == "edit" for c in calls if len(c) > 1)
    assert not any(c[1] == "comment" for c in calls if len(c) > 1)


def test_an_issue_already_in_the_target_state_is_only_commented(monkeypatch):
    """Idempotent: the event can be re-delivered, and a no-op edit is still an edit."""
    calls = _record(monkeypatch, "OPEN", ["state:release-ready"])

    handle_pr_state.set_state(7, "state:release-ready", "shipped")

    assert not any(len(c) > 1 and c[1] == "edit" for c in calls)
    assert any(len(c) > 1 and c[1] == "comment" for c in calls)
