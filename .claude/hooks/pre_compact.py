#!/usr/bin/env python3
from __future__ import annotations

import json
import time

from common import *


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    if not root:
        return 0
    session = str(event.get("session_id") or "unknown")
    issue_no = current_issue(root)
    checkpoint = {
        "version": 1,
        "time": int(time.time()),
        "trigger": event.get("trigger"),
        "session_id": session,
        "branch": branch(root),
        "issue": issue_no,
        "commit": short_sha(root),
        "status": git(root, "status", "--short")[:4000],
        "diff_stat": git(root, "diff", "--stat")[:2500],
        "staged_stat": git(root, "diff", "--cached", "--stat")[:2500],
        "changed_files": changed_files(root)[:200],
    }
    if issue_no:
        issue = gh_issue(root, issue_no)
        if issue:
            body = str(issue.get("body", ""))
            checkpoint["issue_state"] = {
                "title": issue.get("title"),
                "updatedAt": issue.get("updatedAt"),
                "current": section(body, "Current implementation state", 900),
                "problems": section(body, "Known problems", 600),
                "next": section(body, "Next exact actions", 600),
            }
    path = state_dir(root) / "checkpoints" / f"{session}.json"
    try:
        path.write_text(json.dumps(checkpoint, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        emit({"decision": "block", "reason": f"Could not write compaction checkpoint: {exc}"})
        return 0
    log_event(
        root,
        "PreCompact",
        {"session_id": session, "issue": issue_no, "trigger": event.get("trigger"), "checkpoint": str(path)},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
