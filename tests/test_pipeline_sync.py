"""The drift report, which is only worth having if it can report drift.

Every case here builds two checkouts and compares them, because the failure this guards
against is the report coming back clean for a copy that is behind -- and a clean report is
what a working one looks like too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import pipeline_sync  # noqa: E402


def plane(root: Path, *, hook: str = "print('deny')\n", extra: dict[str, str] | None = None) -> Path:
    """A checkout carrying enough of the plane for the comparison to have something to do."""
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / ".claude" / "hooks" / "pre_tool_policy.py").write_text(hook, encoding="utf-8")
    (root / "workflow").mkdir()
    (root / "workflow" / "self_test.py").write_text("check()\n", encoding="utf-8")
    (root / "ci").mkdir()
    (root / "ci" / "run.py").write_text("run()\n", encoding="utf-8")
    (root / "flow").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    for name, text in (extra or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_identical_checkouts_report_no_drift(tmp_path, capsys):
    upstream = plane(tmp_path / "upstream")
    local = plane(tmp_path / "local")

    assert pipeline_sync.report(upstream, local) == 0
    assert "match" in capsys.readouterr().out


def test_a_changed_file_is_reported_and_fails(tmp_path, capsys):
    """The case the whole script exists for: a fix upstream that never arrived."""
    upstream = plane(tmp_path / "upstream", hook="print('deny')\nprint('and audit')\n")
    local = plane(tmp_path / "local")

    assert pipeline_sync.report(upstream, local) == 1
    assert "drifted: .claude/hooks/pre_tool_policy.py" in capsys.readouterr().out


def test_a_file_the_copy_never_received_is_reported(tmp_path, capsys):
    upstream = plane(tmp_path / "upstream", extra={"workflow/pin_actions.py": "pin()\n"})
    local = plane(tmp_path / "local")

    assert pipeline_sync.report(upstream, local) == 1
    assert "missing: workflow/pin_actions.py" in capsys.readouterr().out


def test_a_file_only_the_copy_has_is_reported_separately(tmp_path, capsys):
    """Not the same finding: a consumer's own script is not an upstream file gone stale."""
    upstream = plane(tmp_path / "upstream")
    local = plane(tmp_path / "local", extra={"scripts/deploy-thing": "#!/bin/sh\n"})

    assert pipeline_sync.report(upstream, local) == 1
    assert "extra:   scripts/deploy-thing" in capsys.readouterr().out


@pytest.mark.parametrize("name", pipeline_sync.EXCLUDED)
def test_consumer_owned_files_are_not_drift(tmp_path, name):
    """Differing here is the adoption working. Reporting it teaches people to ignore this."""
    upstream = plane(tmp_path / "upstream", extra={name: "upstream\n"})
    local = plane(tmp_path / "local", extra={name: "the consumer's own\n"})

    assert pipeline_sync.report(upstream, local) == 0


def test_configuration_and_tests_are_out_of_scope(tmp_path):
    """`.claude-workflow.json` is where a consumer is supposed to differ."""
    upstream = plane(
        tmp_path / "upstream",
        extra={".claude-workflow.json": '{"a": 1}\n', "tests/test_x.py": "assert True\n"},
    )
    local = plane(
        tmp_path / "local",
        extra={".claude-workflow.json": '{"a": 2}\n', "tests/test_x.py": "assert False\n"},
    )

    assert pipeline_sync.report(upstream, local) == 0


def test_line_endings_alone_are_not_drift(tmp_path):
    """A Windows checkout differs from a Linux one in every byte and no line."""
    upstream = plane(tmp_path / "upstream")
    local = plane(tmp_path / "local")
    (local / "workflow" / "self_test.py").write_bytes(b"check()\r\n")

    assert pipeline_sync.report(upstream, local) == 0


def test_an_upstream_with_nothing_in_it_is_an_error_not_a_pass(tmp_path, capsys):
    """A wrong --upstream path would otherwise report every file as clean."""
    empty = tmp_path / "empty"
    empty.mkdir()

    assert pipeline_sync.report(empty, plane(tmp_path / "local")) == 2
    assert "no control-plane files" in capsys.readouterr().out


def test_the_upstream_directory_must_exist(tmp_path, capsys):
    assert pipeline_sync.main(["--check", "--upstream", str(tmp_path / "absent")]) == 2
    assert "is not a directory" in capsys.readouterr().out


def test_this_repository_is_its_own_upstream(tmp_path):
    """Run against itself, the plane reports no drift -- and finds files to compare."""
    assert pipeline_sync.portable_files(ROOT), "no portable files found in this repository"
    assert pipeline_sync.report(ROOT, ROOT) == 0
