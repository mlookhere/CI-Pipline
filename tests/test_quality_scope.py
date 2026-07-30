"""Tests for the quality gate's changed-file scope resolution.

The gate previously could not distinguish "nothing changed" from "git could not
tell me what changed", so an unresolvable base ref produced an empty scope and a
passing gate that had inspected nothing. These tests pin the distinction.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ci"))

# E402 is unavoidable here, not preference: ci/ is a scripts directory, not an
# importable package, so sys.path must be extended before the import can resolve.
# Scoped to this one line rather than exempted in pyproject.toml.
import quality  # noqa: E402

GIT_IDENTITY = [
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repository with one commit, wired in as quality.ROOT."""
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "kept.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "initial")
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    monkeypatch.delenv("CI_BASE_REF", raising=False)
    return tmp_path


def test_staged_but_uncommitted_file_is_in_scope(repo, monkeypatch):
    """The regression: `git add` then run the gate used to inspect nothing."""
    (repo / "added.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "added.py")
    monkeypatch.setenv("CI_BASE_REF", "HEAD")
    files, _ = quality.files_for_mode("changed")
    assert "added.py" in files


def test_unstaged_file_is_in_scope(repo, monkeypatch):
    (repo / "kept.py").write_text("VALUE = 99\n", encoding="utf-8")
    monkeypatch.setenv("CI_BASE_REF", "HEAD")
    files, _ = quality.files_for_mode("changed")
    assert "kept.py" in files


def test_committed_diff_against_base_is_in_scope(repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "second.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    monkeypatch.setenv("CI_BASE_REF", base)
    files, against = quality.files_for_mode("changed")
    assert "second.py" in files
    assert against == base


def test_single_commit_repo_falls_back_to_all_tracked_files(repo, monkeypatch):
    """HEAD has no parent, so an empty scope would be a lie: everything is new."""
    monkeypatch.setenv("CI_BASE_REF", "refs/heads/does-not-exist")
    files, against = quality.files_for_mode("changed")
    assert "kept.py" in files
    assert "no parent" in against


def test_unresolvable_base_with_a_parent_reports_the_substitution(repo, monkeypatch, capsys):
    (repo / "second.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    monkeypatch.setenv("CI_BASE_REF", "refs/heads/does-not-exist")
    files, against = quality.files_for_mode("changed")
    assert against == "HEAD~1"
    assert "second.py" in files
    # The narrowing must be announced, not applied silently.
    assert "does not resolve" in capsys.readouterr().err


def test_git_failure_raises_instead_of_reporting_an_empty_scope(tmp_path, monkeypatch):
    """A non-repository must be an error, never an empty scope."""
    monkeypatch.setattr(quality, "ROOT", tmp_path)
    with pytest.raises(quality.ScopeError) as excinfo:
        quality.git_lines("ls-files", "--error-unmatch", "nope")
    assert "failed with exit" in str(excinfo.value)


def test_main_fails_when_the_scope_cannot_be_determined(repo, monkeypatch, capsys):
    def explode(_mode):
        raise quality.ScopeError("git is unavailable")

    monkeypatch.setattr(quality, "files_for_mode", explode)
    monkeypatch.setattr(sys, "argv", ["quality.py", "--mode", "changed"])
    assert quality.main() == 1
    captured = capsys.readouterr()
    assert "UNDETERMINED" in captured.err
    assert "refusing to report success" in captured.err


def test_main_reports_an_empty_scope_explicitly(repo, monkeypatch, capsys):
    monkeypatch.setattr(quality, "files_for_mode", lambda _mode: ([], "HEAD"))
    monkeypatch.setattr(sys, "argv", ["quality.py", "--mode", "changed"])
    assert quality.main() == 0
    assert "nothing in scope" in capsys.readouterr().out


def test_excluded_paths_are_dropped_from_scope(repo, monkeypatch):
    monkeypatch.setattr(quality, "QUALITY", {"exclude_globs": ["ignored/**"]})
    (repo / "ignored").mkdir()
    (repo / "ignored" / "skip.py").write_text("VALUE = 4\n", encoding="utf-8")
    (repo / "counted.py").write_text("VALUE = 5\n", encoding="utf-8")
    git(repo, "add", "-A")
    monkeypatch.setenv("CI_BASE_REF", "HEAD")
    files, _ = quality.files_for_mode("changed")
    assert "counted.py" in files
    assert not any(path.startswith("ignored/") for path in files)
