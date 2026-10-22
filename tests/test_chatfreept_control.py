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


def test_utf8_subprocess_output_preserves_unicode():
    import sys

    emitter = "import sys; sys.stdout.buffer.write('dash \\u2014 quote \\u201d'.encode('utf-8'))"
    assert control.output([sys.executable, "-c", emitter]) == "dash \u2014 quote \u201d"


def test_nonzero_subprocess_output_is_not_success():
    import sys

    assert control.output([sys.executable, "-c", "import sys; sys.exit(1)"]) == ""


def test_uncapturable_output_fails_closed(monkeypatch):
    import subprocess

    import pytest

    monkeypatch.setattr(
        control, "shell", lambda *_args, **_kwargs: subprocess.CompletedProcess(["gh"], 0, None, "")
    )
    with pytest.raises(SystemExit):
        control.output(["gh", "issue", "view", "102"])


def test_control_block_handles_unicode_and_literal_backslashes():
    examples = ("State \u2014 Ready", r"Volume F:\Projects\workspace", r"Backreference \1 and \g<0>")
    original = f"Header\n{control.CONTROL_START}\nold\n{control.CONTROL_END}\nFooter"
    for example in examples:
        fresh = f"{control.CONTROL_START}\n{example}\n{control.CONTROL_END}"
        rendered = control.replace_control_block(original, fresh)
        assert example in rendered
        assert rendered.startswith("Header\n") and rendered.endswith("\nFooter")
        assert rendered.count(control.CONTROL_START) == 1


def test_a_missing_control_block_preserves_owner_notes():
    result = control.replace_control_block(
        "Owner notes \u2014 keep\n", control.CONTROL_START + "new" + control.CONTROL_END
    )
    assert "Owner notes \u2014 keep" in result
    assert "Previous control notes" in result


def test_newly_lossy_issue_body_never_reaches_github(monkeypatch):
    import pytest

    commands = []
    monkeypatch.setattr(control, "shell", lambda command, **_kwargs: commands.append(command))
    with pytest.raises(SystemExit):
        control.write_issue_body(102, "corrupted \ufffd content", prefix="test-")
    assert commands == []


def test_preexisting_replacement_character_is_not_a_false_positive(monkeypatch):
    commands = []
    monkeypatch.setattr(control, "shell", lambda command, **_kwargs: commands.append(command))
    text = "previously corrupted \ufffd entry"
    control.write_issue_body(102, text, original=text, prefix="test-")
    assert len(commands) == 1
    assert commands[0][:5] == ["gh", "issue", "edit", "102", "--body-file"]


def test_write_is_byte_identical_and_never_passes_body_as_argv(monkeypatch):
    body = "First \u2014 line\nSecond \u201d line\nTrailing\n"
    uploaded = []
    commands = []

    def capture(command, **_kwargs):
        commands.append(command)
        uploaded.append(Path(command[-1]).read_bytes())

    monkeypatch.setattr(control, "shell", capture)
    control.write_issue_body(102, body, prefix="test-")
    assert uploaded == [body.encode("utf-8")]
    assert "--body" not in commands[0]
    assert commands[0][-2] == "--body-file"
    assert not Path(commands[0][-1]).exists()


def test_control_sync_is_idempotent_and_does_not_overwrite_notes(monkeypatch):
    import json

    body = "Owner note\n" + control.CONTROL_START + "\nfixed\n" + control.CONTROL_END
    replacement = control.CONTROL_START + "\nfixed\n" + control.CONTROL_END
    writes = []
    monkeypatch.setattr(control, "find_control_issue", lambda *_args: {"number": 1})
    monkeypatch.setattr(control, "open_issues_and_prs", lambda: ([], {}))
    monkeypatch.setattr(control, "collect_active", lambda *_args: ([], []))
    monkeypatch.setattr(control, "render_control_block", lambda *_args: replacement)
    monkeypatch.setattr(control, "output", lambda *_args: json.dumps({"body": body}))
    monkeypatch.setattr(control, "write_control_body", lambda *args, **kwargs: writes.append((args, kwargs)))
    control.sync_control({"branches": {"production": "main"}, "github": {}})
    assert writes == []
