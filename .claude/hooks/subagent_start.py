#!/usr/bin/env python3
from common import *

ROLE = {
    "pr-explorer": "Read only. Map changed code and dependencies; return evidence, not a fix.",
    "reviewer": "Read only. Prioritize correctness, security, regressions, and missing tests. Avoid style-only findings.",
    "test-triage": "Read only. Reproduce the smallest failing test, identify root cause, and return exact commands and evidence. Do not retry blindly.",
    "security-reviewer": "Read only. Trace trust boundaries, authorization, secret handling, injection, and unsafe defaults. Mark uncertainty.",
    "migration-reviewer": "Read only. Check forward compatibility, old-app/new-schema behavior, data loss, locks, and rollback assumptions.",
    "Explore": "Read only. Map execution paths and return concise evidence with file paths and symbols. Do not edit or write Issue state.",
    "Plan": "Read only. Gather only the evidence needed for a bounded implementation plan.",
}


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    agent_type = str(event.get("agent_type") or "default")
    text = ROLE.get(
        agent_type,
        (
            "Stay within the delegated scope. Prefer read-heavy work. Return a distilled handback with "
            "Findings, Evidence, and Recommendation; do not dump raw logs or alter GitHub Issues."
        ),
    )
    if root:
        log_event(
            root,
            "SubagentStart",
            {
                "session_id": event.get("session_id"),
                "agent_id": event.get("agent_id"),
                "agent_type": agent_type,
            },
        )
        text += f"\nParent task: Issue #{current_issue(root) or 'unassigned'}, branch {branch(root)}, commit {short_sha(root)}."
    emit({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": text}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
