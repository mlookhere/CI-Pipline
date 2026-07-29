#!/usr/bin/env python3
from common import *


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    if not root:
        return 0
    path = state_dir(root) / "checkpoints" / f"{event.get('session_id') or 'unknown'}.json"
    log_event(
        root,
        "PostCompact",
        {
            "session_id": event.get("session_id"),
            "issue": current_issue(root),
            "trigger": event.get("trigger"),
            "checkpoint_exists": path.exists(),
        },
    )
    if not path.exists():
        emit(
            {
                "continue": True,
                "systemMessage": "Compaction completed without a local checkpoint; refresh the controlling Issue and git state before editing.",
            }
        )
    else:
        emit({"continue": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
