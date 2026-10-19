"""Agent-runtime and vendoring boundary regression tests (#102)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_runtime_audit as audit  # noqa: E402


@pytest.mark.parametrize(
    "name",
    [
        ".claude/settings.json",
        "scripts/claude-exec",
        "workflow/claude_flow.py",
        ".github/workflows/claude-review.yml",
    ],
)
def test_legacy_runtime_path_cannot_survive(name, tmp_path):
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("deprecated\n", encoding="utf-8")
    assert any(name.split("/")[0] in item for item in audit.verify(tmp_path))


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("ci/run", "exec claude --dangerous\n"),
        ("workflow/runner.py", 'os.execvp("claude", ["claude"])\n'),
        ("scripts/bootstrap", 'CLAUDE_CI_HOME="$HOME/no"\n'),
        (".github/workflows/agent.yml", "    - uses: anthropics/claude-code-action@v1\n"),
        ("workflow/state.py", 'CONFIG = ROOT / ".claude-workflow.json"\n'),
    ],
)
def test_agent_execution_or_auth_reference_is_forbidden(name, content):
    assert audit.forbidden_text(name, content)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("ci/run", 'exec "$(resolve_python)" ci/run.py "$@"\n'),
        ("workflow/main.py", 'print("no secondary autonomous agent")\n'),
        (".github/workflows/ci.yml", "    - uses: actions/checkout@deadbeef\n"),
    ],
)
def test_deterministic_runtime_actions_remain_allowed(name, content):
    assert audit.forbidden_text(name, content) == []


def test_not_claimed_ready_until_legacy_runtime_is_gone():
    current = audit.verify(ROOT)
    assert any("legacy runtime path is still present" in finding for finding in current)


def test_legacy_agent_invoker_is_not_an_approved_entry_point():
    from self_test import CONTROL_PLANE_EXECUTABLES

    assert "scripts/claude-exec" not in CONTROL_PLANE_EXECUTABLES
    assert "scripts/bootstrap" in CONTROL_PLANE_EXECUTABLES
    assert "ci/run" in CONTROL_PLANE_EXECUTABLES
