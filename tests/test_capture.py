"""Regressions for the capture wrapper's printed summary (Issue #77).

`.claude/bin/capture.py` is what every long command in a session is routed through: it runs
the command, writes the full log to `.git/claude/logs`, and prints a bounded summary. The
decode side was already hardened -- the child is read as UTF-8 with `errors="replace"` --
but the encode side was not, so a character the console codec cannot carry killed the
wrapper itself and the command's own result was lost behind a traceback. A byte-order mark
out of a GitHub Actions job log did exactly that, and it is not only the BOM: the U+FFFD
that `errors="replace"` leaves behind for an undecodable byte is equally unencodable in
cp1252, so both appear in the log these tests capture.

The stream is supplied here rather than inherited. CI runs on Linux, where stdout is
already UTF-8, so a test that prints to the ambient stream passes with or without the fix
and proves nothing. Same technique as the Issue #67 cases in tests/test_claude_flow.py.
"""

from __future__ import annotations

import base64
import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "bin"))
sys.path.insert(0, str(ROOT / "workflow"))

# E402 is suppressed for that reason alone: neither `.claude/bin` nor `workflow/` is an
# importable package, so the path has to be extended before these imports can resolve. Same
# pattern as tests/test_claude_flow.py and tests/test_pre_tool_policy.py.
import bash_tools  # noqa: E402
import capture  # noqa: E402
import claude_flow  # noqa: E402
import local_metrics  # noqa: E402

BOM = "﻿"
REPLACEMENT = "�"
BOM_LINES = 6
# Six lines carrying a BOM, the way every GitHub Actions job log line does, then one
# carrying a byte no UTF-8 decoder accepts. The wrapper turns that byte into U+FFFD, which
# a cp1252 stdout rejects just as it rejects the BOM.
EMITTER = r"""printf '\xef\xbb\xbfline %s\n' 1 2 3 4 5 6; printf 'undecodable \x9d byte\n'"""


def _stream(buffer: io.BytesIO, encoding: str) -> io.TextIOWrapper:
    """The stream the wrapper is actually handed: its stdout is a pipe under the Claude Code
    runtime, so Python encodes it with the locale codec -- cp1252 on Windows. The buffer is
    passed in rather than kept on the wrapper so the bytes can be read back without going
    through `.buffer`, whose type says nothing about what backs it."""
    return io.TextIOWrapper(buffer, encoding=encoding, errors="surrogateescape")


def _run_capture(tmp_path: Path, monkeypatch, encoding: str, extra: list[str]) -> tuple[int, str]:
    """Drive `capture.main()` end to end inside a throwaway repository.

    A real repository because the wrapper resolves its log directory through `git`, and a
    throwaway one so the run cannot prune logs this session captured for real.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    printed = io.BytesIO()
    stdout = _stream(printed, encoding)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", _stream(io.BytesIO(), encoding))
    encoded = base64.urlsafe_b64encode(EMITTER.encode()).decode()
    monkeypatch.setattr(sys, "argv", ["capture.py", "--encoded", encoded, *extra])

    code = capture.main()

    stdout.flush()
    return code, printed.getvalue().decode("utf-8")


@pytest.mark.parametrize(
    "encoding",
    [
        pytest.param("cp1252", id="windows-pipe"),
        pytest.param("utf-8", id="linux-runner"),
    ],
)
def test_a_captured_log_full_of_byte_order_marks_prints_whole(encoding, tmp_path, monkeypatch):
    """The observed failure: `gh run view --job <id> --log` through the wrapper died on the
    BOM every Actions log line carries, so a command that succeeded reported as a traceback.
    """
    code, printed = _run_capture(tmp_path, monkeypatch, encoding, [])

    assert code == 0, "the captured command succeeded, so the wrapper must report success"
    assert printed.count(BOM) == BOM_LINES, "a BOM the stream codec cannot encode must not be dropped"
    assert REPLACEMENT in printed, "the marker standing in for an undecodable byte must survive too"
    for number in range(1, BOM_LINES + 1):
        assert f"{BOM}line {number}" in printed, "no captured line may lose characters on the way out"


def test_a_truncated_summary_prints_both_of_its_slices(tmp_path, monkeypatch):
    """A job log is longer than the summary budget, so the branch that actually runs in
    anger is the truncated one -- three more prints the fix has to cover, not just the one
    in the traceback. Head reaches the first captured line, tail the last."""
    code, printed = _run_capture(tmp_path, monkeypatch, "cp1252", ["--head", "4", "--tail", "3"])

    assert code == 0
    assert "lines omitted" in printed, "this budget must exercise the truncating branch"
    assert f"{BOM}line 1" in printed, "the head slice must print"
    assert REPLACEMENT in printed, "the tail slice must print"


def test_a_usage_error_still_leaves_a_stream_that_can_report_it(tmp_path, monkeypatch):
    """`use_utf8_streams()` is main()'s *first* statement, not merely somewhere above the
    prints. argparse writes usage to stderr and raises SystemExit without ever reaching
    them, and the interpreter writes any traceback from `raise SystemExit(main())` to that
    same stderr -- so a call placed after `parse_args()` would leave both on the locale
    codec, and the wrapper would once again die while reporting rather than while working.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", _stream(io.BytesIO(), "cp1252"))
    errors = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", _stream(errors, "cp1252"))
    monkeypatch.setattr(sys, "argv", ["capture.py"])  # --encoded is required

    with pytest.raises(SystemExit):
        capture.main()

    # The write is the assertion: on a stderr still encoding as cp1252 it raises
    # UnicodeEncodeError, which is the whole defect, one stream over.
    sys.stderr.write(f"{BOM}{REPLACEMENT}\n")
    sys.stderr.flush()
    reported = errors.getvalue().decode("utf-8")
    assert "usage:" in reported, "the usage error itself must still reach the stream"
    assert f"{BOM}{REPLACEMENT}" in reported, "and stderr must carry what stdout can"


def test_every_entry_point_shares_one_stream_fix():
    """Three processes print captured output -- the wrapper, the controller, and the metrics
    reporter it re-execs -- and a second copy of the helper is how the next one drifts out
    of step with the others. That drift is this Issue: Issue #67 fixed the controller, and
    the copy it left behind was never going to reach the wrapper."""
    assert capture.use_utf8_streams is bash_tools.use_utf8_streams
    assert claude_flow.use_utf8_streams is bash_tools.use_utf8_streams
    assert local_metrics.use_utf8_streams is bash_tools.use_utf8_streams
