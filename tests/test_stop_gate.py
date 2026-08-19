"""The completion gate, which decides whether a claim of "done" is backed by anything.

Every stage writes the same report shape whether it passed or failed, so the only thing
separating evidence from its opposite is what this module reads out of the file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

# E402: the hooks directory is not an importable package, so sys.path has to be extended
# first. Suppressed for that reason alone, matching tests/test_pre_tool_policy.py.
import stop_gate  # noqa: E402


def repository(tmp_path: Path) -> Path:
    """A real one-commit repository, because `report_fresh` asks git for HEAD."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "file.txt").write_text("content\n", encoding="utf-8")
    # The reports this fixture writes live under artifacts/, ignored in every real
    # repository. Without that here, writing evidence makes the tree dirty and the
    # dirty-tree reason fires in tests that are about something else entirely.
    (root / ".gitignore").write_text("artifacts/" + chr(10), encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "initial"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root


def head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def write_report(root: Path, name: str = "fast", **fields) -> Path:
    reports = root / "artifacts" / "ci"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{name}.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def test_a_passing_report_for_head_counts(tmp_path):
    root = repository(tmp_path)
    write_report(root, success=True, commit=head(root), stage="fast")

    assert stop_gate.report_fresh(root) is True


def test_a_failing_report_is_not_evidence(tmp_path):
    """The defect: a gate that had just gone red satisfied the completion check."""
    root = repository(tmp_path)
    write_report(root, success=False, commit=head(root), stage="fast")

    assert stop_gate.report_fresh(root) is False


def test_a_report_for_a_different_commit_is_not_evidence(tmp_path):
    """Freshness by modification time made a stale report look current."""
    root = repository(tmp_path)
    write_report(root, success=True, commit="0" * 40, stage="fast")

    assert stop_gate.report_fresh(root) is False


def test_one_passing_report_is_enough_among_failures(tmp_path):
    """Stages are recorded separately; a failed `release` does not erase a passed `fast`."""
    root = repository(tmp_path)
    write_report(root, name="release", success=False, commit=head(root))
    write_report(root, name="fast", success=True, commit=head(root))

    assert stop_gate.report_fresh(root) is True


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"commit": "HEAD"}, id="no-success-key"),
        pytest.param({"success": True}, id="no-commit-key"),
        pytest.param({"success": "true", "commit": "HEAD"}, id="success-as-a-string"),
        pytest.param({"success": 1, "commit": "HEAD"}, id="success-as-a-number"),
    ],
)
def test_a_report_that_does_not_say_it_passed_is_not_evidence(tmp_path, fields):
    """`is True`, not truthiness: a report that never recorded a verdict has not given one."""
    root = repository(tmp_path)
    resolved = {key: (head(root) if value == "HEAD" else value) for key, value in fields.items()}
    write_report(root, **resolved)

    assert stop_gate.report_fresh(root) is False


def test_no_reports_directory_is_not_evidence(tmp_path):
    assert stop_gate.report_fresh(repository(tmp_path)) is False


def test_an_unreadable_report_does_not_crash_the_gate(tmp_path):
    """A hook that raises here stops enforcing anything at all."""
    root = repository(tmp_path)
    reports = root / "artifacts" / "ci"
    reports.mkdir(parents=True)
    (reports / "broken.json").write_text("{not json", encoding="utf-8")

    assert stop_gate.report_fresh(root) is False


def test_a_broken_report_alongside_a_good_one_still_passes(tmp_path):
    root = repository(tmp_path)
    reports = root / "artifacts" / "ci"
    reports.mkdir(parents=True)
    (reports / "broken.json").write_text("[]", encoding="utf-8")
    write_report(root, success=True, commit=head(root))

    assert stop_gate.report_fresh(root) is True


# --------------------------------------------- where a controlling Issue is required


def _gate(monkeypatch, root, *, branch_name, message="All done.", issue=None, issue_body=""):
    """Drive `main()` with everything it reads stubbed except the decision under test."""
    emitted = []
    monkeypatch.setattr(
        stop_gate,
        "read_event",
        lambda: {"cwd": str(root), "last_assistant_message": message, "session_id": "s"},
    )
    monkeypatch.setattr(stop_gate, "git_root", lambda *_: root)
    monkeypatch.setattr(stop_gate, "branch", lambda *_: branch_name)
    monkeypatch.setattr(stop_gate, "current_issue", lambda *_: issue)
    monkeypatch.setattr(
        stop_gate, "config", lambda *_: {"branches": {"integration": "dev", "production": "master"}}
    )
    monkeypatch.setattr(stop_gate, "fresh_issue", lambda *_: {"body": issue_body})
    monkeypatch.setattr(stop_gate, "log_event", lambda *_, **__: None)
    monkeypatch.setattr(stop_gate, "emit", emitted.append)
    stop_gate.main()
    return emitted[0]


def _blocked(payload) -> str:
    return payload.get("reason", "") if payload.get("decision") == "block" else ""


@pytest.mark.parametrize("branch_name", ["dev", "master"])
def test_finished_work_on_a_settled_branch_is_allowed(tmp_path, monkeypatch, branch_name):
    """The end state the gate could not express: merged, clean, gated, and said so."""
    root = repository(tmp_path)
    write_report(root, success=True, commit=head(root))

    assert _blocked(_gate(monkeypatch, root, branch_name=branch_name)) == ""


def test_a_dirty_tree_on_a_settled_branch_is_still_refused(tmp_path, monkeypatch):
    root = repository(tmp_path)
    write_report(root, success=True, commit=head(root))
    (root / "file.txt").write_text("edited\n", encoding="utf-8")

    assert "dirty" in _blocked(_gate(monkeypatch, root, branch_name="dev"))


def test_a_failing_report_on_a_settled_branch_is_still_refused(tmp_path, monkeypatch):
    root = repository(tmp_path)
    write_report(root, success=False, commit=head(root))

    assert "no fresh" in _blocked(_gate(monkeypatch, root, branch_name="dev"))


def test_a_task_branch_with_no_issue_is_still_refused(tmp_path, monkeypatch):
    """The case the requirement was written for, and the only one it now applies to."""
    root = repository(tmp_path)
    write_report(root, success=True, commit=head(root))

    assert "controlling Issue" in _blocked(_gate(monkeypatch, root, branch_name="rename-things"))


def test_a_task_branch_whose_issue_omits_the_commit_is_still_refused(tmp_path, monkeypatch):
    root = repository(tmp_path)
    write_report(root, success=True, commit=head(root))

    payload = _gate(monkeypatch, root, branch_name="work/7-x", issue=7, issue_body="no sha here")

    assert "does not reference the current commit" in _blocked(payload)


def test_a_task_branch_whose_issue_names_the_commit_is_allowed(tmp_path, monkeypatch):
    root = repository(tmp_path)
    write_report(root, success=True, commit=head(root))

    payload = _gate(monkeypatch, root, branch_name="work/7-x", issue=7, issue_body=head(root))

    assert _blocked(payload) == ""


def test_saying_nothing_about_completion_is_never_blocked(tmp_path, monkeypatch):
    """The gate reads a claim, not a turn: no claim, no evidence required."""
    root = repository(tmp_path)

    payload = _gate(monkeypatch, root, branch_name="rename-things", message="Still working on it.")

    assert _blocked(payload) == ""
