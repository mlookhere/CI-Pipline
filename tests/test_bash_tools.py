"""The bash resolver added by Issue #35.

`bash` on PATH is the WSL launcher on many Windows installs. It runs, so every name-based
check passes, but it runs a different operating system: Windows environment variables do not
cross into it, so a stage command arrives with an empty `$PROJECT_PYTHON` and every gate dies
as `: command not found`. These tests pin the property the resolver actually selects on,
rather than a guess about which path is which.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import bash_tools  # noqa: E402


def test_the_resolved_bash_receives_an_exported_variable():
    """The one property that matters, asserted against the real resolved shell."""
    assert bash_tools.passes_environment(bash_tools.bash_command())


def test_something_that_is_not_bash_is_rejected():
    """The check is behavioural, so it rejects an interpreter that cannot honour the call."""
    assert not bash_tools.passes_environment(sys.executable)


def test_a_missing_candidate_is_rejected_without_raising():
    assert not bash_tools.passes_environment(str(ROOT / "no" / "such" / "bash"))


def test_the_probe_does_not_hand_the_child_process_the_ambient_environment(monkeypatch):
    """Credentials must not reach a subprocess environment, probe or not."""
    monkeypatch.setenv("KNOWLEDGE_NEXUS_FAKE_TOKEN", "must-not-propagate")
    environment = bash_tools.probe_environment()
    assert "KNOWLEDGE_NEXUS_FAKE_TOKEN" not in environment
    assert environment[bash_tools.PROBE_VARIABLE] == bash_tools.PROBE_VALUE
    assert set(environment) <= {bash_tools.PROBE_VARIABLE, "PATH", "SystemRoot", "SYSTEMROOT"}


def test_an_explicit_override_is_preferred(monkeypatch):
    monkeypatch.setenv("CLAUDE_BASH", "/opt/chosen/bash")
    assert bash_tools.bash_candidates()[0] == "/opt/chosen/bash"


def test_candidates_are_unique_and_non_empty(monkeypatch):
    monkeypatch.delenv("CLAUDE_BASH", raising=False)
    candidates = bash_tools.bash_candidates()
    assert candidates, "at least the bash on PATH should be offered"
    assert len(candidates) == len(set(candidates))
    assert all(candidate for candidate in candidates)


def test_the_wsl_launcher_is_not_what_gets_chosen():
    """On Windows `System32\\bash.exe` is first on PATH, and it must lose to a bash that
    shares this process's environment. Asserted unconditionally: on any other platform those
    directories cannot appear, so the assertion costs nothing and cannot rot into a skip."""
    chosen = Path(bash_tools.bash_command()).resolve()
    parts = {part.lower() for part in chosen.parts}
    assert "system32" not in parts
    assert "windowsapps" not in parts
