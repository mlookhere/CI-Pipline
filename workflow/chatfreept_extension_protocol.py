"""Offline Chat FreePT continuation marker validation; never authorizes CI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SPEC = Path(__file__).resolve().parents[1] / ".chatfreept" / "extension.json"
FENCE = re.compile(
    r"(?s)" + re.escape(chr(96) * 3) + r"chatfreept[^\n]*\n(.*?)\n" + re.escape(chr(96) * 3) + r"\s*$"
)
MARKER = re.compile(
    r"^CHATFREEPT_STATUS:\s*(Continue|Needs input|Plan ready|Testing|Complete|Error)\s*$", re.I
)
FIELD = re.compile(r"^([A-Z]+):\s*(.+?)\s*$")
COUNTS = re.compile(
    r"^(\d+) required\s*/\s*(\d+) passed\s*/\s*(\d+) pending\s*/\s*(\d+) failed(?:\b.*)?$", re.I
)
ACTIONS = {
    "Continue": "resume",
    "Needs input": "pause_for_user",
    "Plan ready": "begin_development",
    "Testing": "pause_for_validation",
    "Complete": "audit_before_stop",
    "Error": "stop_on_error",
}
FIELDS = frozenset(
    ("V", "PHASE", "REPO", "ITEM", "NOTE", "URL", "BRANCH", "ISSUE", "PR", "CI", "CHECKS", "ACTIONS")
)
CI_VALUES = frozenset(("Not run", "Queued", "Running", "Passed", "Failed", "Cancelled", "Unknown"))


def protocol() -> dict[str, Any]:
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("statuses") != list(ACTIONS):
        raise ValueError("unsupported extension protocol")
    return data


def valid_url(value: str, repository: str, path_kind: str) -> bool:
    url = urlsplit(value)
    prefix = f"/{repository}/"
    if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment:
        return False
    if not url.path.startswith(prefix):
        return False
    return (
        path_kind == "any"
        or re.fullmatch(rf"/{re.escape(repository)}/{path_kind}/[1-9]\d*", url.path) is not None
    )


def fields_from_marker(message: str) -> tuple[str, dict[str, str]]:
    if not isinstance(message, str) or len(message) > 65536:
        raise ValueError("missing or oversized reply")
    text = message.strip()
    fenced = FENCE.search(text)
    if fenced is not None:
        text = fenced.group(1)
    elif chr(96) * 3 in text or not text.startswith("CHATFREEPT_STATUS:"):
        raise ValueError("final marker is not readable")
    lines = text.splitlines()
    if not lines or len(lines) > 28:
        raise ValueError("invalid marker length")
    match = MARKER.fullmatch(lines[0].strip())
    if match is None:
        raise ValueError("unsupported status marker")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        parsed = FIELD.fullmatch(line.strip())
        if parsed is None:
            raise ValueError("unexpected content in status block")
        key, value = parsed.groups()
        if key not in FIELDS or key in fields:
            raise ValueError("duplicate or unknown status field")
        fields[key] = value
    return match.group(1).capitalize(), fields


def check_counts(fields: dict[str, str]) -> list[str]:
    if "CHECKS" not in fields:
        return []
    matched = COUNTS.fullmatch(fields["CHECKS"])
    if matched is None:
        return ["free-form CHECKS is display only; obtain fresh GitHub evidence"]
    required, passed, pending, failed = (int(part) for part in matched.groups())
    if not required or passed + pending + failed != required:
        raise ValueError("required checks are zero or counts do not reconcile")
    if fields.get("CI") == "Passed" and (pending or failed or passed != required):
        raise ValueError("reported Passed with incomplete required checks")
    return []


def interpret(message: str, repository: str) -> dict[str, Any]:
    """Hints only; GitHub required checks, PR state and protection remain authoritative."""
    meta = protocol()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("selected repository must be owner/name")
    status, fields = fields_from_marker(message)
    if fields.get("V") != "1" or fields.get("REPO") != repository:
        raise ValueError("status version or repository lock mismatch")
    if fields.get("PHASE") not in ("Planning", "Developing", "Testing", "Complete"):
        raise ValueError("invalid or missing phase")
    if not fields.get("NOTE"):
        raise ValueError("missing NOTE")
    if fields.get("CI", "Unknown") not in CI_VALUES:
        raise ValueError("unsupported CI display status")
    for key, kind in (("ISSUE", "issues"), ("PR", "pull"), ("ACTIONS", "actions/runs"), ("URL", "any")):
        if key in fields and not valid_url(fields[key], repository, kind):
            raise ValueError(f"{key} points outside selected GitHub repository")
    warnings = check_counts(fields)
    if fields.get("CI") == "Passed":
        warnings.append("model-reported CI is advisory; verify live required checks on exact head")
    if status == "Complete":
        warnings.append("audit open Issues, PRs and required release checks before stopping")
    return {
        "version": meta["version"],
        "status": status,
        "action_hint": ACTIONS[status],
        "repository": repository,
        "fields": fields,
        "warnings": warnings,
        "merge_authorized": False,
        "release_authorized": False,
        "completion_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Chat FreePT status validator.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--input", type=argparse.FileType("r", encoding="utf-8"), default=sys.stdin)
    args = parser.parse_args()
    try:
        result = interpret(args.input.read(), args.repo)
    except (OSError, ValueError) as exc:
        print(f"extension handoff rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
