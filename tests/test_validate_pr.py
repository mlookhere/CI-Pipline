"""The `PR metadata` gate, which decides whether a pull request may merge and had no tests.

It is the only required check that reads intent rather than running code, and it is the one
that refuses a change for reasons a person has to act on. Both directions matter here: what it
lets through is a merge nobody reviewed, and what it refuses wrongly is a pull request nobody
can fix -- which is exactly how every Dependabot update came to be permanently blocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import validate_pr  # noqa: E402

BOT = "dependabot[bot]"
MANIFEST = "ci/requirements-ci.txt"
WORKFLOW = ".github/workflows/ci-pr.yml"

HUMAN_BODY = """Refs #7

## Result
The gate accepts a compliant pull request.

## Implementation
One function, one call site.

## Verification
`./ci/run fast` and `./ci/run pr` both exit 0.

## Risk
None beyond the changed paths, which carry their labels.

## Remaining work
None.
"""


def event(tmp_path, monkeypatch, *, login: str, base: str = "dev", body: str = "", head: str) -> None:
    payload = {
        "pull_request": {
            "number": 7,
            "body": body,
            "head": {"ref": head},
            "base": {"ref": base},
            "user": {"login": login},
        }
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))


def answers(monkeypatch, *, files: list[str], pr_labels: list[str], issue: dict | None = None) -> None:
    """Stand in for the two `gh` reads, so the gate is tested and GitHub is not."""

    def fake(*args: str):
        if args[0] == "pr":
            return {
                "files": [{"path": path} for path in files],
                "labels": [{"name": name} for name in pr_labels],
                "additions": 1,
                "deletions": 1,
                "changedFiles": len(files),
                "isDraft": False,
            }
        return issue or {"state": "OPEN", "title": "t", "labels": [], "body": ""}

    monkeypatch.setattr(validate_pr, "gh_json", fake)


# --------------------------------------------------------- who counts as automated


@pytest.mark.parametrize("login", sorted(validate_pr.AUTOMATED_AUTHORS))
def test_a_bot_pull_request_into_integration_is_automated(login):
    assert validate_pr.automated({"user": {"login": login}}, "dev", "dev") is True


def test_a_bot_pull_request_into_production_is_not():
    """An automated change must not reach production without a release Issue, whoever opened it."""
    assert validate_pr.automated({"user": {"login": BOT}}, "master", "dev") is False


@pytest.mark.parametrize(
    "login",
    [
        pytest.param("a-contributor", id="a-person"),
        pytest.param("dependabot", id="a-login-that-merely-looks-like-it"),
        pytest.param("", id="absent"),
    ],
)
def test_everyone_else_is_not_automated(login):
    assert validate_pr.automated({"user": {"login": login}}, "dev", "dev") is False


def test_a_payload_with_no_user_is_not_automated():
    assert validate_pr.automated({}, "dev", "dev") is False


# --------------------------------------------------------- the gate end to end


def test_a_dependabot_update_passes_on_its_labels_alone(tmp_path, monkeypatch, capsys):
    """The regression this exists for: every dependency update failed a required check."""
    event(tmp_path, monkeypatch, login=BOT, head="dependabot/pip/ci/dev/ruff-gte-0.16.3")
    answers(monkeypatch, files=[MANIFEST], pr_labels=["type:maintenance", "risk:dependencies", "risk:ci"])

    assert validate_pr.main() == 0
    assert "an automated dependency update" in capsys.readouterr().out


def test_a_dependabot_action_bump_still_needs_the_ci_label(tmp_path, monkeypatch, capsys):
    """The exemption covers the author, never the change: workflow edits still need risk:ci."""
    event(tmp_path, monkeypatch, login=BOT, head="dependabot/github_actions/dev/checkout-7.0.1")
    answers(monkeypatch, files=[WORKFLOW], pr_labels=["type:maintenance", "risk:dependencies"])

    assert validate_pr.main() == 1
    assert "risk:ci" in capsys.readouterr().err


def test_a_dependabot_pull_request_into_production_is_still_refused(tmp_path, monkeypatch, capsys):
    event(tmp_path, monkeypatch, login=BOT, base="master", head="dependabot/pip/ci/dev/ruff-gte-0.16.3")
    answers(monkeypatch, files=[MANIFEST], pr_labels=["type:maintenance", "risk:dependencies"])

    assert validate_pr.main() == 1
    assert "Refs #ISSUE" in capsys.readouterr().err


def test_a_person_still_has_to_name_an_issue(tmp_path, monkeypatch, capsys):
    """The exemption must not become a hole: a human pull request is unchanged."""
    event(tmp_path, monkeypatch, login="a-contributor", head="work/7-something")
    answers(monkeypatch, files=[MANIFEST], pr_labels=["risk:dependencies"])

    assert validate_pr.main() == 1
    assert "Refs #ISSUE" in capsys.readouterr().err


def test_a_person_cannot_borrow_the_exemption_with_a_bot_shaped_branch(tmp_path, monkeypatch, capsys):
    """The branch name is attacker-chosen; the login in the event payload is not."""
    event(tmp_path, monkeypatch, login="a-contributor", head="dependabot/pip/ci/dev/ruff-gte-0.16.3")
    answers(monkeypatch, files=[MANIFEST], pr_labels=["risk:dependencies"])

    assert validate_pr.main() == 1
    assert "Refs #ISSUE" in capsys.readouterr().err


def test_a_compliant_human_pull_request_still_passes(tmp_path, monkeypatch, capsys):
    """The other direction: the exemption must not have disabled the ordinary path."""
    event(tmp_path, monkeypatch, login="a-contributor", body=HUMAN_BODY, head="work/7-something")
    answers(
        monkeypatch,
        files=[MANIFEST],
        pr_labels=["risk:dependencies", "risk:ci"],
        issue={
            "state": "OPEN",
            "title": "t",
            "labels": [{"name": "risk:dependencies"}, {"name": "risk:ci"}],
            "body": "",
        },
    )

    assert validate_pr.main() == 0
    assert "Issue #7" in capsys.readouterr().out


def test_a_human_pull_request_on_the_wrong_branch_is_still_refused(tmp_path, monkeypatch, capsys):
    event(tmp_path, monkeypatch, login="a-contributor", body=HUMAN_BODY, head="feature/whatever")
    answers(
        monkeypatch,
        files=[MANIFEST],
        pr_labels=["risk:dependencies", "risk:ci"],
        issue={
            "state": "OPEN",
            "title": "t",
            "labels": [{"name": "risk:dependencies"}, {"name": "risk:ci"}],
            "body": "",
        },
    )

    assert validate_pr.main() == 1
    assert "work/7-" in capsys.readouterr().err
