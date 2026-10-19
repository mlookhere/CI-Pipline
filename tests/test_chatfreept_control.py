"""Read-only contract tests for deterministic GitHub control synchronization."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_control as control  # noqa: E402


def test_control_block_replaces_legacy_without_erasing_user_notes():
    body = "# Owner notes\n\n" + control.LEGACY_START + "\nold state\n" + control.LEGACY_END
    fresh = control.CONTROL_START + "\nnew state\n" + control.CONTROL_END
    actual = control.replace_control_block(body + "\n\n## Keep this\nCustom notes\n", fresh)
    assert actual.count(control.CONTROL_START) == 1
    assert "old state" not in actual
    assert "Custom notes" in actual


def test_skipped_or_missing_checks_are_not_green():
    assert control.check_summary([]) == "not run"
    assert control.check_summary([{"conclusion": "SKIPPED"}]) != "passing"
    assert control.check_summary([{"conclusion": "FAILURE"}]) == "failing"
    assert "not gate-verified" in control.check_summary([{"conclusion": "SUCCESS"}])


def test_issue_mapping_and_native_policy():
    assert control.issue_number_from_branch("work/102-remove-claude") == 102
    assert control.issue_number_from_branch("dev") is None
    assert control.CONFIG_PATH.name == "github.json"
    assert control.load_config()["github"]["control_issue_title"] == "[CONTROL] Current repository state"
