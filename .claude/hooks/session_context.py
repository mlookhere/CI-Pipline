#!/usr/bin/env python3
from __future__ import annotations

from common import *


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    if root is None:
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": "No Git repository detected; repository workflow rules are unavailable.",
                }
            }
        )
        return 0

    cfg = config(root)
    b = branch(root)
    issue_no = current_issue(root)
    session_id = str(event.get("session_id") or "unknown")
    lease_ttl = int(cfg.get("tracking", {}).get("lease_ttl_seconds", 28800))
    context = [
        "Claude Code task control (compact authoritative context):",
        f"- Branch: {b}",
        f"- Commit: {short_sha(root)}",
        f"- Working tree: {'dirty' if git(root, 'status', '--porcelain') else 'clean'}",
        "- GitHub Issue is mutable handoff state; PR is review state; CI is acceptance authority.",
        "- Never create a handoff document. Use ./flow handoff and keep raw logs outside model context.",
    ]

    control = gh_control_issue(root)
    if control:
        hazards = section(str(control.get("body", "")), "Current hazards", 450)
        if hazards:
            context.extend(["", "Repository hazards:", hazards])

    if issue_no is None:
        context.append(
            "- No Issue number is encoded in this branch. Read-only exploration is allowed; durable "
            "mutation requires a controlling Issue."
        )
    else:
        owned, lease = acquire_or_check_lease(root, issue_no, session_id, lease_ttl)
        if owned:
            context.append(f"- Work lease: owned by this session for Issue #{issue_no}.")
        else:
            context.append(
                f"- WORK LEASE CONFLICT: Issue #{issue_no} is active in another session ({compact(str(lease.get('session_id', 'unknown')), 16)}) at {lease.get('cwd', 'unknown')}. Do not mutate until the lease is released or stale."
            )

        issue = gh_issue(root, issue_no)
        if issue:
            body = str(issue.get("body", ""))
            context.extend(
                [
                    "",
                    f"Task #{issue_no}: {compact(str(issue.get('title', '')), 180)}",
                    f"Labels: {', '.join(sorted(label_names(issue))) or 'none'}",
                ]
            )
            for title, label, size in (
                ("Acceptance criteria", "Acceptance", 800),
                ("Current implementation state", "Current state", 650),
                ("Known problems", "Known problems", 450),
                ("Next exact actions", "Next actions", 450),
            ):
                value = (
                    acceptance(body, size) if title == "Acceptance criteria" else section(body, title, size)
                )
                if value:
                    context.extend([f"{label}:", value])
        else:
            context.append(
                f"- Issue #{issue_no} could not be refreshed; use ./flow start {issue_no} before changing scope."
            )

        pr = gh_pr_for_branch(root, b)
        if pr:
            context.extend(
                [
                    "",
                    f"PR #{pr.get('number')}: {compact(str(pr.get('title', '')), 160)}",
                    f"PR state: {pr.get('state')} / checks {check_summary(pr)} / review {pr.get('reviewDecision') or 'none'}",
                    f"PR URL: {pr.get('url')}",
                ]
            )

    checkpoint = state_dir(root) / "checkpoints" / f"{session_id}.json"
    if event.get("source") == "compact" and checkpoint.exists():
        context.append(f"- Compaction checkpoint: {checkpoint}")
    log_event(
        root,
        "SessionStart",
        {"session_id": session_id, "branch": b, "issue": issue_no, "source": event.get("source")},
    )
    limit = int(cfg.get("token_control", {}).get("session_context_max_chars", 7200))
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(context)[:limit],
            }
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
