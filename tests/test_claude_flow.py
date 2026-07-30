"""Control-plane regressions for Issue #35.

The control model treats a GitHub Issue body as handoff truth, and `./flow handoff` reads
that body and writes it back. Anything lossy in that round-trip corrupts the record the
next session resumes from, so these tests pin the round-trip itself rather than the
commands wrapped around it.

Every test here fails against the code before #35.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402 is unavoidable here and suppressed for that reason only: `workflow/` is not an
# importable package, so the path has to be extended before the import can resolve. Same
# pattern as tests/test_workflow_policy.py.
import claude_flow  # noqa: E402

EM_DASH = "\u2014"
CURLY_QUOTE = "\u201d"

# ------------------------------------------------------------------ decoding


def test_captured_output_is_decoded_as_utf8_not_the_locale_codec():
    """The original defect: gh emits UTF-8, `text=True` alone decodes with the code page.

    Read as cp1252 an em dash becomes three characters, and cmd_handoff writes that back.
    """
    emitter = "import sys; sys.stdout.buffer.write('em \\u2014 dash'.encode('utf-8'))"
    assert claude_flow.output([sys.executable, "-c", emitter]) == f"em {EM_DASH} dash"


def test_a_byte_the_locale_codec_rejects_does_not_abort_the_command():
    """Run two of #35 corrupted a body into U+201D, whose UTF-8 0x9d is undefined in
    cp1252. The decode then raised inside subprocess's reader thread, stdout came back
    None with returncode 0, and the next `./flow` read died on `NoneType.strip()`."""
    emitter = "import sys; sys.stdout.buffer.write('quote \\u201d here'.encode('utf-8'))"
    assert claude_flow.output([sys.executable, "-c", emitter]) == f"quote {CURLY_QUOTE} here"


def test_a_failed_command_still_reports_no_output():
    """Callers read "" as a definite absence -- no such branch, no open PR -- so a
    non-zero exit must keep returning it rather than raising."""
    assert claude_flow.output([sys.executable, "-c", "import sys; sys.exit(1)"]) == ""


def test_output_that_could_not_be_captured_fails_loudly(monkeypatch):
    """Reading nothing and failing to read must not be indistinguishable."""

    def unreadable(*_args, **_kwargs):
        return subprocess.CompletedProcess(["gh"], 0, None, "")

    monkeypatch.setattr(claude_flow, "shell", unreadable)
    with pytest.raises(SystemExit):
        claude_flow.output(["gh", "issue", "view", "35"])


# --------------------------------------------------- managed-block round-trip


def _body(state: str) -> str:
    return f"Preamble.\n\n{claude_flow.STATE_START}\n{state}\n{claude_flow.STATE_END}\n\nTrailer.\n"


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(f"Preclearance {EM_DASH} required.", id="em-dash"),
        pytest.param(f"He said {CURLY_QUOTE}done{CURLY_QUOTE}.", id="curly-quote"),
        pytest.param("Ingested a caf\u00e9 menu.pdf", id="accented"),
        pytest.param("Shipped \U0001f680 today.", id="emoji"),
    ],
)
def test_non_ascii_state_survives_the_managed_block_round_trip(state):
    updated = claude_flow.replace_managed_block(_body("stale"), claude_flow.managed_block(state))
    assert state in updated
    assert updated.encode("utf-8").decode("utf-8") == updated


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(r"Clone at F:\PROJECTs\knowledge-nexus", id="windows-path"),
        pytest.param(r"Matched \1 of the pattern", id="group-reference"),
        pytest.param(r"Escaped \\ backslash", id="double-backslash"),
        pytest.param(r"Anchor \g<0> here", id="named-group-reference"),
    ],
)
def test_backslashes_in_state_are_kept_literally(state):
    """re.sub reads a string replacement as a template. A Windows path in handoff state
    raised `bad escape \\P` and aborted the handoff; `\\1` would have substituted a capture
    group into the record instead."""
    updated = claude_flow.replace_managed_block(_body("stale"), claude_flow.managed_block(state))
    assert state in updated


def test_control_block_replacement_also_keeps_backslashes():
    body = f"Intro\n{claude_flow.CONTROL_START}\nstale\n{claude_flow.CONTROL_END}\n"
    replacement = f"{claude_flow.CONTROL_START}\nC:\\Users\\ci\n{claude_flow.CONTROL_END}"
    assert "C:\\Users\\ci" in claude_flow.replace_control_block(body, replacement)


def test_replacing_a_missing_block_appends_it_without_mangling_text():
    state = f"New {EM_DASH} state with C:\\path"
    updated = claude_flow.replace_managed_block("Body with no markers.\n", claude_flow.managed_block(state))
    assert state in updated
    assert updated.startswith("Body with no markers.")


# ------------------------------------------------------------- portability


def test_a_ci_stage_runs_through_the_python_entry_point():
    """`ci/run` is an extensionless bash script; CreateProcess ignores its shebang and
    raises WinError 193, so ./flow pr died before pushing or opening a PR."""
    command = claude_flow.stage_command("fast")
    assert command[0] == sys.executable
    assert command[1].endswith("run.py"), "the bash shim is not executable on Windows"
    assert Path(command[1]).is_file()
    assert command[2] == "fast"


def test_a_venv_tool_is_found_in_either_layout(tmp_path):
    """doctor reported a provisioned CI runtime as missing because it only looked in
    bin/, the POSIX layout."""
    windows = tmp_path / "win-venv"
    (windows / "Scripts").mkdir(parents=True)
    (windows / "Scripts" / "pre-commit.exe").write_text("", encoding="utf-8")
    assert claude_flow.venv_tool(windows, "pre-commit") is not None

    posix = tmp_path / "posix-venv"
    (posix / "bin").mkdir(parents=True)
    (posix / "bin" / "pre-commit").write_text("", encoding="utf-8")
    assert claude_flow.venv_tool(posix, "pre-commit") is not None

    assert claude_flow.venv_tool(tmp_path / "absent", "pre-commit") is None
