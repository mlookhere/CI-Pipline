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
