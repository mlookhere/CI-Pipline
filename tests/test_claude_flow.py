"""Control-plane regressions for Issue #35.

The control model treats a GitHub Issue body as handoff truth, and `./flow handoff` reads
that body and writes it back. Anything lossy in that round-trip corrupts the record the
next session resumes from, so these tests pin the round-trip itself rather than the
commands wrapped around it.

Every test here fails against the code before #35.
"""

from __future__ import annotations

import json
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


def test_a_body_that_could_not_be_fully_decoded_is_never_written_back(monkeypatch):
    """errors="replace" stops an undecodable byte from killing the read, which would
    otherwise turn a lossy read into a silent write over handoff truth."""
    written: list[list[str]] = []
    monkeypatch.setattr(claude_flow, "shell", lambda command, **_: written.append(command))
    with pytest.raises(SystemExit):
        claude_flow.write_issue_body(35, "state with � in it", prefix="test-")
    assert written == [], "the edit must not be attempted at all"


def test_a_clean_body_is_written_through_a_file_not_an_argument(monkeypatch):
    """Passing a body as argv risks length limits and shell quoting; --body-file does not."""
    written: list[list[str]] = []
    monkeypatch.setattr(claude_flow, "shell", lambda command, **_: written.append(command))
    claude_flow.write_issue_body(35, f"clean {EM_DASH} body", prefix="test-")
    assert len(written) == 1
    assert written[0][:5] == ["gh", "issue", "edit", "35", "--body-file"]
    assert Path(written[0][5]).name.startswith("test-")


def test_the_uploaded_file_is_byte_identical_to_the_body(monkeypatch):
    """The whole point of Issue #35: what GitHub stores must be what was read.

    A UTF-8 codec is not sufficient on its own. A temp file opened in text mode translates
    every LF to CRLF on Windows, so the upload differs from the source and every later cycle
    adds another CR to handoff truth.
    """
    body = f"Line one\nPreclearance {EM_DASH} required.\nTrailing\n"
    uploaded: list[bytes] = []
    monkeypatch.setattr(
        claude_flow, "shell", lambda command, **_: uploaded.append(Path(command[-1]).read_bytes())
    )
    claude_flow.write_issue_body(35, body, prefix="test-")
    assert uploaded == [body.encode("utf-8")]


def test_a_replacement_character_that_was_already_there_may_be_written_back(monkeypatch):
    """Refusing this too would make a body corrupted by the pre-#35 code unrepairable, and
    would let one stray character in an unrelated Issue title wedge every command."""
    written: list[list[str]] = []
    monkeypatch.setattr(claude_flow, "shell", lambda command, **_: written.append(command))
    corrupted = "state with � in it"
    claude_flow.write_issue_body(35, corrupted, prefix="test-", original=corrupted)
    assert len(written) == 1, "a write that adds no loss must go through"


def test_a_replacement_character_this_write_introduces_is_still_refused(monkeypatch):
    written: list[list[str]] = []
    monkeypatch.setattr(claude_flow, "shell", lambda command, **_: written.append(command))
    with pytest.raises(SystemExit):
        claude_flow.write_issue_body(35, "now � lossy", prefix="test-", original="was clean")
    assert written == []


def _pr_outputs(view: str) -> object:
    """Stand in for `output()` across the three gh reads open_pr_for_branch makes."""

    def responder(command: list[str], **_: object) -> str:
        if "issue" in command:
            return view
        return "[]" if "--limit" not in command else '[{"number": 99, "url": "http://pr/99"}]'

    return responder


def test_a_pr_body_is_written_through_a_file_not_an_argument(monkeypatch):
    """The PR is review truth, and its body is spliced from an Issue read with
    errors="replace" -- so it needs the same guarded writer the Issue body uses."""
    written: list[list[str]] = []
    monkeypatch.setattr(claude_flow, "shell", lambda command, **_: written.append(command))
    monkeypatch.setattr(
        claude_flow,
        "output",
        _pr_outputs(json.dumps({"title": "t", "body": f"## Objective\n\nShip {EM_DASH} it.", "labels": []})),
    )
    pr = claude_flow.open_pr_for_branch(35, "work/35-x", {"branches": {"integration": "dev"}})
    assert pr["number"] == 99
    assert len(written) == 1
    assert "--body" not in written[0], "a PR body must not travel through argv"
    assert written[0][-2] == "--body-file"


def test_a_failed_issue_read_stops_the_pr_instead_of_raising_json_errors(monkeypatch):
    """This runs after `git push`, so an uncaught JSONDecodeError leaves a published branch
    with no PR and no explanation."""
    monkeypatch.setattr(claude_flow, "shell", lambda command, **_: None)
    monkeypatch.setattr(claude_flow, "output", _pr_outputs(""))
    with pytest.raises(SystemExit):
        claude_flow.open_pr_for_branch(35, "work/35-x", {"branches": {"integration": "dev"}})


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
