#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))


def gh(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["gh", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=capture
    )
    if result.returncode != 0:
        if capture:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip() if capture else ""


def issue_from_pr(pr: dict[str, Any], integration: str, production: str) -> int | None:
    base = str(pr["base"]["ref"])
    head = str(pr["head"]["ref"])
    if base == integration:
        match = re.search(r"(?:^|/)(\d+)(?:-|$)", head)
        if match:
            return int(match.group(1))
    if base == production:
        match = re.search(r"\b(?:Refs|Fixes|Closes)\s+#(\d+)\b", pr.get("body") or "", re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def set_state(issue: int, target: str, comment: str) -> None:
    details = json.loads(gh("issue", "view", str(issue), "--json", "state,labels", capture=True))
    if details.get("state") != "OPEN":
        print(f"Issue #{issue} is not open; state transition skipped.")
        return
    current = {item["name"] for item in details.get("labels", [])}
    state_labels = set(CONFIG.get("github", {}).get("state_labels", []))
    command = ["issue", "edit", str(issue)]
    for label in sorted((current & state_labels) - {target}):
        command.extend(["--remove-label", label])
    if target not in current:
        command.extend(["--add-label", target])
    if len(command) > 3:
        gh(*command)
    gh("issue", "comment", str(issue), "--body", comment)
    print(f"Issue #{issue} moved to {target}.")


def main() -> int:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if event.get("action") != "closed":
        print("No closed pull request event; nothing to do.")
        return 0
    pr = event["pull_request"]
    integration = CONFIG["branches"]["integration"]
    production = CONFIG["branches"]["production"]
    base = str(pr["base"]["ref"])
    issue = issue_from_pr(pr, integration, production)
    if issue is None:
        print("No controlling Issue could be inferred; state transition skipped.")
        return 0

    merged = bool(pr.get("merged"))
    pr_number = int(pr["number"])
    pr_url = str(pr["html_url"])
    if base == integration:
        if merged:
            set_state(
                issue,
                "state:release-ready",
                f"PR [#{pr_number}]({pr_url}) merged into `{integration}`. This Issue is waiting for release/deployment as applicable.",
            )
        else:
            set_state(
                issue,
                "state:active",
                f"PR [#{pr_number}]({pr_url}) closed without merge. Implementation returned to active state.",
            )
    elif base == production:
        if merged:
            set_state(
                issue,
                "state:release-ready",
                f"Release PR [#{pr_number}]({pr_url}) merged into `{production}`. Production deployment and health verification are pending.",
            )
        else:
            set_state(
                issue,
                "state:active",
                f"Release PR [#{pr_number}]({pr_url}) closed without merge. Release coordination returned to active state.",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
