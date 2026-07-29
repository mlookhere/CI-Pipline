#!/usr/bin/env python3
import os
import re

from common import *

BLOCK_ALWAYS = re.compile(
    r"(?i)(?:\bgh\s+(?:secret|variable)\s+(?:set|delete)\b|\bgh\s+api\b.*(?:rulesets|protection|environments|actions/secrets)|\bgit\s+push\b.*--force|\bdocker\s+run\b.*(?:/var/run/docker\.sock|--privileged)|\b(?:terraform|tofu)\s+destroy\b|\bkubectl\s+delete\b.*(?:namespace|cluster)|\baws\s+.*delete|\bgcloud\s+.*delete|\baz\s+.*delete)"
)
RELEASE_ONLY = re.compile(
    r"(?i)(?:\bssh\b|\bscp\b|\brsync\b.*(?:prod|production)|\b(?:kubectl|helm|terraform|tofu|aws|gcloud|az)\b|\./ops/(?:deploy|rollback)|\./ci/(?:deploy|rollback))"
)


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    if BLOCK_ALWAYS.search(command):
        if root:
            log_event(
                root,
                "PolicyDecision",
                {
                    "session_id": event.get("session_id"),
                    "issue": current_issue(root),
                    "decision": "deny",
                    "category": "privileged-destructive",
                },
            )
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "deny",
                        "message": "This privileged/destructive operation is outside the standard agent workflow. Perform it manually through the documented administrative process.",
                    },
                }
            }
        )
        return 0
    if RELEASE_ONLY.search(command) and os.environ.get("CLAUDE_WORKFLOW_MODE") != "release":
        if root:
            log_event(
                root,
                "PolicyDecision",
                {
                    "session_id": event.get("session_id"),
                    "issue": current_issue(root),
                    "decision": "deny",
                    "category": "release-profile-required",
                },
            )
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "deny",
                        "message": "Infrastructure and deployment commands require a supervised release shell: export CLAUDE_WORKFLOW_MODE=release, then restart Claude Code.",
                    },
                }
            }
        )
        return 0
    if root:
        log_event(
            root,
            "PermissionRequest",
            {
                "session_id": event.get("session_id"),
                "issue": current_issue(root),
                "tool": event.get("tool_name"),
                "deferred": True,
            },
        )
    # No output: preserve the normal human approval prompt.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
