#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys

from common import *


def find_exit(value):
    if isinstance(value, dict):
        for key in ("exit_code", "exitCode", "status_code", "statusCode"):
            if isinstance(value.get(key), int):
                return value[key]
        for child in value.values():
            found = find_exit(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = find_exit(child)
            if found is not None:
                return found
    return None


def failure_count(root: Path, command: str, failed: bool) -> int:
    path = state_dir(root) / "failures.json"
    key = hashlib.sha256(re.sub(r"\s+", " ", command.strip()).encode()).hexdigest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    # `[]` and `"x"` parse cleanly and then raise AttributeError on `.get`, which nothing here
    # catches. Truncated or concurrently written state is exactly what a crash and restart
    # leaves behind, so the failing input is the one this file is most likely to meet.
    # `stop_gate.fresh_issue` already guards the identical shape (Issue #4).
    if not isinstance(data, dict):
        data = {}
    if failed:
        data[key] = int(data.get(key, 0)) + 1
    else:
        data.pop(key, None)
    path.write_text(json.dumps(data), encoding="utf-8")
    return int(data.get(key, 0))


def record_outcome(
    root: Path, event: dict, hook_event: str, tool: str, command: str, exit_code: int | None
) -> None:
    response = event.get("tool_response")
    error_text = str(event.get("error") or "")
    log_event(
        root,
        hook_event,
        {
            "session_id": str(event.get("session_id") or "unknown"),
            "issue": current_issue(root),
            "tool": tool,
            "exit_code": exit_code,
            "input_chars": len(command),
            "response_chars": len(json.dumps(response, default=str))
            if response is not None
            else len(error_text),
        },
    )


def blind_retry_block(root: Path, command: str, failed: bool) -> bool:
    """True when the same command has failed enough times to stop the loop."""
    count = failure_count(root, command, failed)
    limit = int(config(root).get("quality", {}).get("max_identical_command_failures", 3))
    if count < limit:
        return False
    emit(
        {
            "decision": "block",
            "reason": (
                "The same command has failed repeatedly. Stop retrying it unchanged: inspect the first causal "
                "error, reduce the reproduction, and record a blocker if the environment or requirement is wrong."
            ),
        }
    )
    return True


def validate_changed_files(root: Path) -> None:
    paths = changed_files(root)
    if not paths:
        return
    validator = root / ".claude" / "bin" / "quick_validate.py"
    result = subprocess.run(
        [sys.executable, str(validator), *paths],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=12,
    )
    feedback = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        emit(
            {
                "decision": "block",
                "reason": (
                    "Immediate changed-file validation failed. Fix these issues before continuing broad edits:\n"
                    + feedback[:3500]
                ),
            }
        )
    elif feedback:
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": feedback[:1800],
                }
            }
        )


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    if not root:
        return 0
    hook_event = str(event.get("hook_event_name") or "PostToolUse")
    failed_event = hook_event == "PostToolUseFailure"
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    session_id = str(event.get("session_id") or "unknown")

    heartbeat_lease(root, current_issue(root), session_id)
    exit_code = find_exit(event.get("tool_response"))
    if failed_event and exit_code is None:
        exit_code = 1
    record_outcome(root, event, hook_event, tool, command, exit_code)

    # Both shells, from the one taxonomy in common: a command retried unchanged until it
    # passes is the same loop whichever shell runs it, and until Issue #59 the PowerShell
    # tool matched no matcher, so this hook never saw its retries at all.
    if tool in COMMAND_TOOLS and command:
        if blind_retry_block(root, command, failed_event or exit_code not in (None, 0)):
            return 0

    if not failed_event and tool in WRITE_TOOLS:
        validate_changed_files(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
