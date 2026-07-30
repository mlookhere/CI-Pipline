#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
LINK_RE = re.compile(r"\b(?:Refs|Fixes|Closes)\s+#(\d+)\b", re.IGNORECASE)
STANDARD_SECTIONS = ("Result", "Implementation", "Verification", "Risk", "Remaining work")
PLACEHOLDER_PATTERNS = (
    r"^describe\b",
    r"^list\b",
    r"^anything deliberately deferred\b",
    r"^write `none`\b",
    r"^issue_number$",
)


def gh_json(*args: str) -> Any:
    result = subprocess.run(
        ["gh", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout)


def labels_for(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("name", "")) for item in payload.get("labels", [])}


def section(body: str, heading: str) -> str:
    match = re.search(rf"(?ims)^##+\s+{re.escape(heading)}\s*$\n(.*?)(?=^##+\s|\Z)", body)
    return match.group(1).strip() if match else ""


def substantive(value: str) -> bool:
    cleaned = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL).strip()
    cleaned = re.sub(r"(?m)^\s*[-*]\s*\[[ xX]\]\s*", "", cleaned).strip()
    if not cleaned:
        return False
    lowered = cleaned.casefold()
    return not any(re.match(pattern, lowered) for pattern in PLACEHOLDER_PATTERNS)


def path_requires_label(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def check_branch_and_body(
    body: str, head: str, base: str, integration: str, branch_prefix: str, issue_number: int | None
) -> list[str]:
    """Task-branch naming and required PR sections, enforced on integration PRs."""
    if base != integration:
        return []
    failures = []
    if issue_number is not None:
        expected_prefix = f"{branch_prefix}/{issue_number}-"
        if not head.startswith(expected_prefix):
            failures.append(
                f"Task branch must start with `{expected_prefix}` so the branch, Issue, worktree, and PR stay correlated."
            )
    for heading in STANDARD_SECTIONS:
        if not substantive(section(body, heading)):
            failures.append(f"PR section `## {heading}` is missing or still contains placeholder text.")
    return failures


def check_issue(
    issue: dict[str, Any],
    issue_labels: set[str],
    issue_number: int,
    base: str,
    integration: str,
    production: str,
) -> list[str]:
    failures = []
    if str(issue.get("state", "")).upper() != "OPEN":
        failures.append(
            f"Controlling Issue #{issue_number} must remain open until the change is merged and verified."
        )
    if base == production and "type:release" not in issue_labels:
        failures.append(
            f"Production PRs must reference an open Issue labeled `type:release`; Issue #{issue_number} is not."
        )
    if base == integration and "type:release" in issue_labels:
        failures.append("A task PR into the integration branch must not use a release-control Issue.")
    return failures


def check_risk_labels(
    changed: list[str], pr_labels: set[str], issue_labels: set[str], issue_number: int | None
) -> list[str]:
    failures = []
    for required_label, patterns in CONFIG.get("github", {}).get("risk_paths", {}).items():
        if not any(path_requires_label(path, list(patterns)) for path in changed):
            continue
        if required_label not in pr_labels:
            failures.append(f"Changed paths require PR label `{required_label}`.")
        if issue_number is not None and required_label not in issue_labels:
            failures.append(
                f"Changed paths require controlling Issue #{issue_number} to carry `{required_label}`."
            )
    return failures


def check_size(
    changed_files: int,
    changed_lines: int,
    pr_labels: set[str],
    issue_labels: set[str],
    issue_number: int | None,
) -> list[str]:
    quality = CONFIG.get("quality", {})
    too_large = changed_files > int(quality.get("max_pr_files", 80)) or changed_lines > int(
        quality.get("max_pr_changed_lines", 4000)
    )
    if not too_large:
        return []
    size = f"{changed_files} file(s), {changed_lines} changed line(s)"
    failures = []
    if "risk:large-change" not in pr_labels:
        failures.append(f"Large PR ({size}) requires PR label `risk:large-change` or should be split.")
    if issue_number is not None and "risk:large-change" not in issue_labels:
        failures.append(
            f"Large PR ({size}) requires controlling Issue #{issue_number} to carry `risk:large-change`."
        )
    return failures


def main() -> int:
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    event = json.loads(event_path.read_text(encoding="utf-8"))
    pr = event["pull_request"]
    number = int(pr["number"])
    body = pr.get("body") or ""
    head = str(pr["head"]["ref"])
    base = str(pr["base"]["ref"])
    integration = CONFIG["branches"]["integration"]
    production = CONFIG["branches"]["production"]
    branch_prefix = str(CONFIG.get("worktrees", {}).get("branch_prefix", "work")).rstrip("/")
    failures: list[str] = []

    issue_match = LINK_RE.search(body)
    issue_number: int | None = int(issue_match.group(1)) if issue_match else None
    if issue_number is None:
        failures.append("PR body must contain `Refs #ISSUE`, `Fixes #ISSUE`, or `Closes #ISSUE`.")

    failures += check_branch_and_body(body, head, base, integration, branch_prefix, issue_number)
    if base == production and head != integration:
        failures.append(f"Production PRs must originate from `{integration}`, not `{head}`.")

    details = gh_json(
        "pr",
        "view",
        str(number),
        "--json",
        "files,labels,additions,deletions,changedFiles,isDraft",
    )
    changed = [str(item["path"]) for item in details.get("files", [])]
    pr_labels = labels_for(details)
    if details.get("isDraft") and base == production:
        failures.append(
            "The production release PR must be marked ready for review before release metadata can pass."
        )

    issue_labels: set[str] = set()
    if issue_number is not None:
        issue = gh_json("issue", "view", str(issue_number), "--json", "state,title,labels,body")
        issue_labels = labels_for(issue)
        failures += check_issue(issue, issue_labels, issue_number, base, integration, production)

    failures += check_risk_labels(changed, pr_labels, issue_labels, issue_number)

    changed_files = int(details.get("changedFiles") or len(changed))
    changed_lines = int(details.get("additions") or 0) + int(details.get("deletions") or 0)
    failures += check_size(changed_files, changed_lines, pr_labels, issue_labels, issue_number)

    if failures:
        print("PR metadata validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(
        f"PR metadata validation passed for Issue #{issue_number}: "
        f"{changed_files} file(s), {changed_lines} changed line(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
