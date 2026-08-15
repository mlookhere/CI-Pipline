"""The PreToolUse risk-label guard sees the file a write tool is about to touch (Issue #52).

Before this fix `missing_risk_labels` built its path list only from `*** Update File:`
headers in a command string, so an ordinary Edit or Write -- the way every session edits
files -- reached the guard with an empty list and was allowed without the Issue ever being
read. Hosted CI caught the missing label later, after the edit, the commit and the push.

These tests drive the hook's decision function, and `main()` end to end, for both the
denied and the allowed case, so the guard is proven to fail before it is trusted to pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

# E402: the hooks directory is not an importable package, so sys.path has to be extended
# first. Suppressed for that reason alone, matching tests/test_workflow_guards.py.
import pre_tool_policy  # noqa: E402

CFG = {
    "github": {
        "risk_paths": {
            "risk:security": ["**/auth/**", "**/*permission*", "knowledge_nexus/config.py"],
            "risk:ci": [".github/workflows/**", ".claude/**"],
        }
    }
}


def _issue(*labels: str) -> dict:
    return {"number": 52, "labels": [{"name": name} for name in labels]}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A checkout root that is not a git repository: the decision function must not need one."""
    (tmp_path / "knowledge_nexus").mkdir()
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    return tmp_path


# --- the decision function -----------------------------------------------------------


def test_an_edit_of_a_risk_path_without_the_label_is_denied(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue("type:bug"))
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is not None
    assert "risk:security" in reason
    assert "#52" in reason


def test_the_same_edit_with_the_label_present_is_allowed(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue("risk:security"))
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is None


def test_a_write_to_a_non_risk_path_never_consults_the_issue(root, monkeypatch):
    def explode(*_):
        raise AssertionError("gh_issue must not be called for a path no risk glob matches")

    monkeypatch.setattr(pre_tool_policy, "gh_issue", explode)
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Write", {"file_path": str(root / "knowledge_nexus" / "pipeline.py")}, 52
    )
    assert reason is None


def test_a_notebook_edit_under_a_risk_directory_is_denied(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue())
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "NotebookEdit", {"notebook_path": str(root / ".claude" / "hooks" / "x.ipynb")}, 52
    )
    assert reason is not None
    assert "risk:ci" in reason


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda r: str(r / "knowledge_nexus" / "config.py"), id="native-absolute"),
        pytest.param(
            lambda r: str(r / "knowledge_nexus" / "config.py").replace("\\", "/"), id="forward-slash"
        ),
        pytest.param(lambda r: "knowledge_nexus/config.py", id="relative"),
    ],
)
def test_every_path_shape_normalises_to_the_repo_relative_posix_form(root, shape):
    assert pre_tool_policy.repo_relative(root, shape(root)) == "knowledge_nexus/config.py"


def test_a_write_outside_the_checkout_is_not_a_risk_path(root, tmp_path_factory, monkeypatch):
    def explode(*_):
        raise AssertionError("a scratch file outside the checkout must not reach the Issue lookup")

    monkeypatch.setattr(pre_tool_policy, "gh_issue", explode)
    elsewhere = tmp_path_factory.mktemp("scratch") / "knowledge_nexus" / "config.py"
    reason = pre_tool_policy.missing_risk_labels(root, CFG, "Write", {"file_path": str(elsewhere)}, 52)
    assert reason is None
    assert pre_tool_policy.repo_relative(root, str(elsewhere)) is None


def test_an_unreadable_issue_fails_closed(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: None)
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is not None
    assert "risk:security" in reason


def test_the_patch_header_form_is_still_recognised(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue())
    command = "*** Begin Patch\n*** Update File: knowledge_nexus/config.py\n@@\n*** End Patch"
    reason = pre_tool_policy.missing_risk_labels(root, CFG, "Edit", {"command": command}, 52)
    assert reason is not None
    assert "risk:security" in reason


def test_a_bash_command_naming_a_workflow_file_is_still_recognised(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue())
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Bash", {"command": "sed -i s/a/b/ .github/workflows/ci-pr.yml"}, 52
    )
    assert reason is not None
    assert "risk:ci" in reason


def test_the_real_configuration_maps_the_incident_path_to_security(root, monkeypatch):
    """The Issue's own example: knowledge_nexus/config.py, edited directly, must need risk:security."""
    cfg = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue("risk:ci"))
    reason = pre_tool_policy.missing_risk_labels(
        root, cfg, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is not None
    assert "risk:security" in reason


# --- main(), end to end --------------------------------------------------------------


def _drive_main(root: Path, monkeypatch, tool_input: dict, labels: tuple[str, ...]) -> list[dict]:
    """Run the hook's entry point on an Edit event and return everything it emitted."""
    emitted: list[dict] = []
    monkeypatch.setattr(
        pre_tool_policy,
        "read_event",
        lambda: {"tool_name": "Edit", "tool_input": tool_input, "session_id": "s", "cwd": str(root)},
    )
    monkeypatch.setattr(pre_tool_policy, "git_root", lambda *_: root)
    monkeypatch.setattr(pre_tool_policy, "config", lambda *_: CFG)
    monkeypatch.setattr(pre_tool_policy, "current_issue", lambda *_: 52)
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_: _issue(*labels))
    monkeypatch.setattr(pre_tool_policy, "foreign_lease", lambda *_: None)
    monkeypatch.setattr(pre_tool_policy, "log_event", lambda *_: None)
    monkeypatch.setattr(pre_tool_policy, "emit", emitted.append)
    assert pre_tool_policy.main() == 0
    return emitted


def _denials(emitted: list[dict]) -> list[dict]:
    return [
        item["hookSpecificOutput"]
        for item in emitted
        if item.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    ]


def test_main_denies_a_direct_edit_of_a_risk_path_without_the_label(root, monkeypatch):
    emitted = _drive_main(root, monkeypatch, {"file_path": str(root / "knowledge_nexus" / "config.py")}, ())
    denials = _denials(emitted)
    assert len(denials) == 1
    assert "risk:security" in denials[0]["permissionDecisionReason"]


def test_main_allows_the_same_edit_when_the_label_is_present(root, monkeypatch):
    emitted = _drive_main(
        root, monkeypatch, {"file_path": str(root / "knowledge_nexus" / "config.py")}, ("risk:security",)
    )
    assert _denials(emitted) == []
