#!/usr/bin/env python3
from __future__ import annotations

import json
import re

from common import *

COMPLETE = re.compile(
    r"(?i)\b(?:done|complete|completed|implemented|fixed|finished|ready for (?:review|merge)|opened (?:the )?PR|all checks pass)\b"
)


def report_fresh(root: Path) -> bool:
    """A gate that passed on this commit -- not merely a file newer than it.

    This compared modification times and never opened the report. Every stage writes the same
    shape whether it passed or failed, so a run that had just gone red satisfied the check
    that exists to confirm the work is finished, and a run against an older commit satisfied
    it too as long as the file was touched afterwards. The one thing a completion claim needs
    to be backed by is the one thing it did not read.

    Both halves are now required: `success` is true, and `commit` is this HEAD. A stale report
    left behind by an earlier commit no longer counts, which is the case that made the
    mtime comparison look like it was working.
    """
    reports = root / "artifacts" / "ci"
    if not reports.is_dir():
        return False
    head = git(root, "rev-parse", "HEAD")
    if not head:
        return False
    for path in sorted(reports.rglob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        if report.get("success") is True and report.get("commit") == head:
            return True
    return False


def fresh_issue(root: Path, issue: int) -> dict:
    result = run(["gh", "issue", "view", str(issue), "--json", "body,state,updatedAt"], cwd=root, timeout=12)
    if result.returncode != 0:
        return {}
    try:
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    message = str(event.get("last_assistant_message") or "")
    if not root:
        emit({"continue": True})
        return 0
    issue_no = current_issue(root)
    completion_claim = bool(COMPLETE.search(message))
    log_event(
        root,
        "Stop",
        {
            "session_id": event.get("session_id"),
            "issue": issue_no,
            "completion_claim": completion_claim,
            "dirty": bool(git(root, "status", "--porcelain")),
        },
    )
    if event.get("stop_hook_active") or not completion_claim:
        emit({"continue": True})
        return 0

    reasons: list[str] = []
    if git(root, "status", "--porcelain"):
        reasons.append("the working tree is dirty")
    if issue_no is None:
        reasons.append("there is no controlling Issue")
    if not report_fresh(root):
        reasons.append("there is no fresh machine-readable local CI report for the current commit")
    if issue_no:
        issue = fresh_issue(root, issue_no)
        body = str(issue.get("body", ""))
        full_sha = git(root, "rev-parse", "HEAD")
        if not issue:
            reasons.append("the controlling Issue could not be refreshed")
        elif full_sha not in body and short_sha(root) not in body:
            reasons.append("the Issue handoff does not reference the current commit")

    if reasons:
        emit(
            {
                "decision": "block",
                "reason": "Before claiming completion, close the evidence gap: "
                + "; ".join(reasons)
                + (
                    ". Inspect the diff, run the required ./ci/run gates, and update the existing Issue "
                    "with ./flow handoff. Do not merely restate completion."
                ),
            }
        )
        return 0
    emit({"continue": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
