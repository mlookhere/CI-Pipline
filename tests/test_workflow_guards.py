"""Tests for the fast-gate guards added by Issue #35.

The guards run against the whole repository on every fast gate, which means a guard that
quietly stopped recognising anything would report success forever. These tests pin the
recognition itself, so the gate cannot rot into a no-op.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import self_test  # noqa: E402


def _rejects(source: str) -> bool:
    """Exactly the decision the gate makes, on exactly one call.

    Asserting against the keyword set instead would let the test pass while the gate
    disagreed -- which is how `stdout=subprocess.DEVNULL` came to be documented as accepted
    while it was in fact rejected.
    """
    calls = self_test.subprocess_reads(ast.parse(source))
    assert len(calls) == 1, f"expected exactly one subprocess call in {source!r}"
    keywords = calls[0][1]
    return (
        self_test.decodes_output(keywords)
        and self_test.captures_output(keywords)
        and not self_test.names_codec(keywords)
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('subprocess.run(["gh"], text=True, capture_output=True)', id="run-capture"),
        pytest.param('subprocess.check_output(["gh"], text=True)', id="check_output"),
        pytest.param('subprocess.Popen(["gh"], stdout=PIPE, text=True)', id="popen-pipe"),
        pytest.param(
            'subprocess.run(["gh"], universal_newlines=True, capture_output=True)', id="legacy-text"
        ),
        pytest.param('subprocess.run(["gh"], text=True, stderr=subprocess.PIPE)', id="stderr-only"),
        pytest.param(
            'subprocess.run(["gh"], text=True, capture_output=True, encoding=None)', id="encoding-none"
        ),
        pytest.param(
            'from subprocess import run\nrun(["gh"], text=True, capture_output=True)', id="from-import"
        ),
        pytest.param(
            'import subprocess as sp\nsp.run(["gh"], text=True, capture_output=True)', id="module-alias"
        ),
        pytest.param("subprocess.run(cmd, text=True, **options)", id="opaque-kwargs"),
        pytest.param('subprocess.run(["gh"], text=True, stdout=handle)', id="unknown-redirect"),
    ],
)
def test_a_decoding_capture_is_rejected(source):
    assert _rejects(source), "this shape decodes captured output with the locale codec"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'subprocess.run(["gh"], text=True, encoding="utf-8", capture_output=True)', id="encoded"
        ),
        pytest.param('subprocess.check_output(["gh"], text=True, encoding="utf-8")', id="encoded-check"),
        pytest.param('subprocess.run(["git", "ls-files", "-z"], capture_output=True)', id="byte-mode"),
        pytest.param('subprocess.run(["gh"], stdout=subprocess.DEVNULL, text=True)', id="discarded-stdout"),
        pytest.param('subprocess.run(["gh"], stderr=subprocess.DEVNULL, text=True)', id="discarded-stderr"),
        pytest.param('subprocess.run(["gh"], stderr=subprocess.STDOUT, text=True)', id="merged-stderr"),
        pytest.param('subprocess.run(["gh"], text=False, capture_output=True)', id="text-switched-off"),
        pytest.param("subprocess.run(cmd, **options)", id="opaque-without-decoding"),
    ],
)
def test_a_call_the_gate_must_leave_alone(source):
    assert not _rejects(source), "the gate must not demand a codec here"


def test_unrelated_run_calls_are_ignored():
    """`uvicorn.run` is not a subprocess read, and neither is a bare `run` with no import."""
    source = "uvicorn.run(app, text=True, capture_output=True)\nrun(['gh'], text=True)"
    assert self_test.subprocess_reads(ast.parse(source)) == []


RUNNER_COMMAND = (
    'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/run" "$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_policy.py"'
)


@pytest.mark.parametrize(
    "interpreter",
    [
        pytest.param("python3", id="store-stub-on-windows"),
        # This one used to be *asserted acceptable* here. It is the default on Debian and
        # Ubuntu without python-is-python3, where it does not exist at all (Issue #38).
        pytest.param("python", id="absent-on-debian"),
    ],
)
def test_a_hook_command_may_not_name_an_interpreter(interpreter):
    """A hook that silently fails to start stops enforcing policy without failing anything."""
    hook = {
        "command": f'{interpreter} "$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_policy.py"',
        "timeout": 10,
    }
    failures = self_test.check_hook_entry("PreToolUse", hook)
    assert any("names an interpreter directly" in failure for failure in failures)


def test_the_runner_form_is_accepted():
    assert self_test.check_hook_entry("PreToolUse", {"command": RUNNER_COMMAND, "timeout": 10}) == []


def test_a_hook_that_bypasses_the_runner_is_rejected():
    """No interpreter named, but nothing resolving one either -- still a silent death."""
    hook = {"command": '"$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_policy.py"', "timeout": 10}
    failures = self_test.check_hook_entry("PreToolUse", hook)
    assert any("does not go through" in failure for failure in failures)


def test_every_referenced_hook_file_is_checked_not_just_the_first():
    """The command names two files now; checking one leaves the other free to vanish."""
    hook = {
        "command": RUNNER_COMMAND.replace("pre_tool_policy.py", "no_such_hook.py"),
        "timeout": 10,
    }
    failures = self_test.check_hook_entry("PreToolUse", hook)
    assert any("references missing no_such_hook.py" in failure for failure in failures)


def test_the_real_settings_file_launches_every_hook_through_the_runner():
    """The regression this Issue exists for, pinned against the file itself."""
    settings = json.loads((self_test.ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert len(commands) == 11, commands
    assert all(self_test.HOOK_RUNNER in command for command in commands)
    assert not any(self_test.BARE_PYTHON.search(command) for command in commands)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("python -c 'print(1)'", id="bare-python-command"),
        pytest.param("python3 -c 'print(1)'", id="bare-python3-command"),
        pytest.param('VALUE="$(python -c pass)"', id="substitution"),
        pytest.param("gh issue list | python -c pass", id="pipeline"),
    ],
)
def test_a_bare_python_invocation_is_recognised(text):
    assert self_test.BARE_PYTHON.search(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("scripts/lib/python.sh", id="the-resolver-itself"),
        pytest.param(". scripts/lib/python.sh", id="sourced-resolver"),
        pytest.param('PY="$(resolve_python)"', id="resolve_python"),
        pytest.param("resolve_system_python", id="resolve_system_python"),
        pytest.param("$PYTHON_BIN", id="PYTHON_BIN"),
        pytest.param(".venv/Scripts/python.exe -m pytest", id="explicit-venv-path"),
        pytest.param("/usr/local/bin/python3 -c pass", id="absolute-path"),
        pytest.param("apt install python-is-python3", id="package-name"),
    ],
)
def test_a_bare_python_match_the_gate_must_not_claim(text):
    assert not self_test.BARE_PYTHON.search(text), text


def test_the_repository_currently_satisfies_both_guards():
    assert self_test.check_subprocess_decoding() == []
    assert self_test.check_entry_point_interpreters() == []


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("python3 -c 'print(1)'", id="command"),
        pytest.param('VALUE="$(python3 -c pass)"', id="substitution"),
        pytest.param("gh issue list | python3 -c pass", id="pipeline"),
    ],
)
def test_a_bare_python3_invocation_is_recognised(line):
    assert self_test.BARE_PYTHON3.search(line)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('. "$ROOT/scripts/lib/python.sh"', id="library-path"),
        pytest.param('PY="$(resolve_python)"', id="resolver"),
        pytest.param('SYSTEM_PYTHON="$(resolve_system_python)"', id="system-resolver"),
        pytest.param('exec "$PY" workflow/claude_lease.py "$@"', id="resolved-interpreter"),
    ],
)
def test_the_resolver_pattern_is_not_mistaken_for_a_bare_interpreter(line):
    assert not self_test.BARE_PYTHON3.search(line)
