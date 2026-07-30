"""Tests for the fast-gate guards added by Issue #35.

The guards run against the whole repository on every fast gate, which means a guard that
quietly stopped recognising anything would report success forever. These tests pin the
recognition itself, so the gate cannot rot into a no-op.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import self_test  # noqa: E402


def _keywords(source: str) -> set[str]:
    calls = self_test.subprocess_reads(ast.parse(source))
    assert len(calls) == 1, f"expected exactly one subprocess call in {source!r}"
    return calls[0][1]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('subprocess.run(["gh"], text=True, capture_output=True)', id="run-capture"),
        pytest.param('subprocess.check_output(["gh"], text=True)', id="check_output"),
        pytest.param('subprocess.Popen(["gh"], stdout=PIPE, text=True)', id="popen-pipe"),
        pytest.param('subprocess.run(["gh"], universal_newlines=True, capture_output=True)', id="legacy-text"),
    ],
)
def test_a_decoding_capture_is_recognised(source):
    keywords = _keywords(source)
    assert {"text", "universal_newlines", "encoding"} & keywords
    assert {"capture_output", "stdout", "__always_captures__"} & keywords
    assert "encoding" not in keywords, "this shape is exactly what the gate must reject"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'subprocess.run(["gh"], text=True, encoding="utf-8", capture_output=True)', id="encoded"
        ),
        pytest.param('subprocess.check_output(["gh"], text=True, encoding="utf-8")', id="encoded-check"),
    ],
)
def test_naming_a_codec_satisfies_the_guard(source):
    assert "encoding" in _keywords(source)


def test_a_byte_mode_capture_is_not_flagged():
    """Bytes are decoded deliberately by the caller, so there is no locale codec involved."""
    keywords = _keywords('subprocess.run(["git", "ls-files", "-z"], capture_output=True)')
    assert not {"text", "universal_newlines", "encoding"} & keywords


def test_a_call_that_discards_output_is_not_flagged():
    keywords = _keywords('subprocess.run(["gh"], stdout=subprocess.DEVNULL, text=True)')
    assert "stdout" in keywords, "it does name stdout"
    # ...but the gate only rejects it when the output is read back, which DEVNULL is not.


def test_unrelated_run_calls_are_ignored():
    """`uvicorn.run` and a bare `run` are not subprocess reads."""
    source = "uvicorn.run(app, text=True, capture_output=True)\nrun(['gh'], text=True)"
    assert self_test.subprocess_reads(ast.parse(source)) == []


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
