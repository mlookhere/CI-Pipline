#!/usr/bin/env python3
from common import *


def main() -> int:
    event = read_event()
    message = str(event.get("last_assistant_message") or "")
    root = git_root(event.get("cwd"))
    if root:
        log_event(
            root,
            "SubagentStop",
            {
                "session_id": event.get("session_id"),
                "agent_id": event.get("agent_id"),
                "agent_type": event.get("agent_type"),
                "message_chars": len(message),
            },
        )
    if event.get("stop_hook_active"):
        emit({"continue": True})
        return 0
    required = ("Findings", "Evidence", "Recommendation")
    missing = [name for name in required if not re.search(rf"(?im)^#+\s*{name}\b|^{name}:\s*", message)]
    if missing or len(message) > 5000:
        reason = (
            "Return one concise structured handback with headings Findings, Evidence, and Recommendation. "
            "Cite paths/symbols and commands; omit raw logs."
        )
        if len(message) > 5000:
            reason += " Keep it under 5,000 characters."
        emit({"decision": "block", "reason": reason})
        return 0
    emit({"continue": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
