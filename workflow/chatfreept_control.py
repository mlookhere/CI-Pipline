#!/usr/bin/env python3
"""Deterministic GitHub-only control Issue sync; never launches another coding agent."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from bash_tools import use_utf8_streams

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".chatfreept" / "github.json"
CONTROL_START = "<!-- chatfreept-control:start -->"
CONTROL_END = "<!-- chatfreept-control:end -->"
LEGACY_START = "<!-- " + "claude-control:start -->"
LEGACY_END = "<!-- " + "claude-control:end -->"
REPLACEMENT = "\ufffd"
ACTIVE_STATES = {"state:active", "state:blocked", "state:review", "state:release-ready"}


def load_config() -> dict[str, Any]:
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot load GitHub control policy: {exc}")
    if not isinstance(value, dict) or not isinstance(value.get("github"), dict):
        fail("GitHub control policy is missing its required github section")
    return value


def shell(
    command: list[str], *, cwd: Path = ROOT, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    if not capture:
        print("$ " + " ".join(shlex.quote(part) for part in command), flush=True)
    # UTF-8 explicitly. `text=True` on its own decodes with the platform preferred
    # encoding, which on Windows is a code page such as cp1252, while GitHub Issue and PR
    # bodies are UTF-8. Read that way an em dash becomes three mojibake characters, and
    # cmd_handoff writes the mangled text straight back, so the corruption persists.
    # errors="replace" keeps an undecodable byte from raising inside subprocess's reader
    # thread, which would otherwise return stdout as None alongside returncode 0.
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )


def output(command: list[str], *, cwd: Path = ROOT) -> str:
    result = shell(command, cwd=cwd, check=False, capture=True)
    if result.returncode != 0:
        return ""
    if result.stdout is None:
        # "Read nothing" and "could not read" must not be indistinguishable. Callers read
        # "" as a definite absence -- no such branch, no open PR, no lease -- and act on
        # it, so a failure to capture has to stop the command instead of impersonating one.
        fail("no output could be captured from: " + " ".join(shlex.quote(part) for part in command))
    return result.stdout.strip()


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def issue_number_from_branch(branch: str) -> int | None:
    match = re.search(r"(?:^|/)(\d+)(?:-|$)", branch)
    return int(match.group(1)) if match else None


def replace_control_block(body: str, replacement: str) -> str:
    for start, end in ((CONTROL_START, CONTROL_END), (LEGACY_START, LEGACY_END)):
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        if pattern.search(body):
            return pattern.sub(lambda _: replacement, body)
    if body.strip():
        return replacement + "\n\n## Previous control notes\n\n" + body.strip() + "\n"
    return replacement + "\n\n## Current hazards\n\nNone recorded.\n"


def labels_for(item: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in item.get("labels", [])}


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def check_summary(rollup: list[dict[str, Any]]) -> str:
    if not rollup:
        return "not run"
    values = {
        str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        for item in rollup
    }
    if values & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}:
        return "failing"
    if values & {"PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING", "EXPECTED"}:
        return "pending"
    if values == {"SUCCESS"}:
        return "observed success (not gate-verified)"
    return "unverified"


def find_control_issue(config: dict[str, Any]) -> dict[str, Any]:
    title = config["github"]["control_issue_title"]
    raw = output(["gh", "issue", "list", "--state", "open", "--limit", "200", "--json", "number,title,url"])
    issues = json.loads(raw or "[]")
    return next((item for item in issues if item.get("title") == title), {})


def remote_branch_sha(branch: str) -> str:
    value = output(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{quote(branch, safe='')}", "--jq", ".commit.sha"]
    )
    return value[:12] if value else "unavailable"


def open_issues_and_prs() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Fetch open Issues and open PRs, keyed by the Issue number in the PR branch name."""
    raw_issues = output(
        ["gh", "issue", "list", "--state", "open", "--limit", "200", "--json", "number,title,labels,url"]
    )
    raw_prs = output(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title,headRefName,baseRefName,url,statusCheckRollup",
        ]
    )
    pr_by_issue: dict[int, dict[str, Any]] = {}
    for pr in json.loads(raw_prs or "[]"):
        inferred = issue_number_from_branch(str(pr.get("headRefName", "")))
        if inferred is not None:
            pr_by_issue[inferred] = pr
    return json.loads(raw_issues or "[]"), pr_by_issue


def active_row(issue: dict[str, Any], pr: dict[str, Any] | None, state_labels: list[str]) -> str:
    labels = labels_for(issue)
    branch = str(pr.get("headRefName", "—")) if pr else "—"
    state = ", ".join(label.removeprefix("state:") for label in state_labels) or "PR open"
    risks = (
        ", ".join(sorted(label.removeprefix("risk:") for label in labels if label.startswith("risk:"))) or "—"
    )
    pr_cell = (
        f"[#{pr['number']}]({pr['url']}) / {check_summary(pr.get('statusCheckRollup', []))}" if pr else "—"
    )
    return (
        f"| [#{issue['number']}]({issue['url']}) {markdown_cell(str(issue['title']))} "
        f"| `{markdown_cell(branch)}` | {markdown_cell(state)} | {markdown_cell(risks)} | {pr_cell} |"
    )


def collect_active(
    issues: list[dict[str, Any]], pr_by_issue: dict[int, dict[str, Any]], control_number: Any
) -> tuple[list[str], list[dict[str, Any]]]:
    rows: list[str] = []
    release_issues: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("number") == control_number:
            continue
        labels = labels_for(issue)
        if "type:release" in labels:
            release_issues.append(issue)
        pr = pr_by_issue.get(int(issue["number"]))
        state_labels = sorted(labels & ACTIVE_STATES)
        if not state_labels and pr is None:
            continue
        rows.append(active_row(issue, pr, state_labels))
    return rows, release_issues


def render_control_block(
    config: dict[str, Any], rows: list[str], release_issues: list[dict[str, Any]]
) -> str:
    current_release = max(release_issues, key=lambda item: int(item["number"])) if release_issues else None
    release_text = (
        f"[#{current_release['number']}]({current_release['url']}) {markdown_cell(str(current_release['title']))}"
        if current_release
        else "none"
    )
    row_limit = int(config.get("tracking", {}).get("control_issue_max_active_rows", 20))
    overflow = max(0, len(rows) - row_limit)
    table = "\n".join(rows[:row_limit]) if rows[:row_limit] else "| — | — | no active task Issues | — | — |"
    if overflow:
        table += f"\n\n_{overflow} additional active Issue(s) omitted; use the `state:*` filters for the full list._"
    production = config["branches"]["production"]
    integration = config["branches"]["integration"]
    return f"""{CONTROL_START}
## Branch state

- Production: `{production}@{remote_branch_sha(production)}`
- Integration: `{integration}@{remote_branch_sha(integration)}`
- Current release Issue: {release_text}

## Active work

| Issue | Branch | State | Risk | PR / CI |
|---|---|---|---|---|
{table}

_Last synchronized by deterministic GitHub Actions control workflow._
{CONTROL_END}"""


def guard_lossy_body(label: str, body: str, original: str | None) -> None:
    """Refuse to persist text that this tool made lossy.

    Reads decode with errors="replace" so an undecodable byte cannot kill the command, but
    that turns a lossy read into a silent write, and an Issue body is handoff truth. A
    replacement character that was not there before means information was lost on the way
    in, so this stops rather than persisting the loss.

    Text that already carried U+FFFD keeps the right to be written back. Refusing that too
    would leave a body corrupted by the pre-#35 code permanently unrepairable, and would let
    one stray character in an unrelated Issue title -- the control body is composed from
    every active Issue's title -- wedge every control-plane command with no way out.
    Counting is deliberately coarse: it cannot tell a moved replacement character from a
    preserved one, and errs toward allowing the write it can prove is not lossier.
    """
    already = (original or "").count(REPLACEMENT)
    carried = body.count(REPLACEMENT)
    if carried > already:
        fail(
            f"refusing to write {label}: {carried - already} character(s) could not be decoded "
            "on the way in. Inspect the source before rerunning."
        )
    if carried:
        print(
            f"warning: {label} already carried {carried} replacement character(s); this write "
            "preserves them rather than introducing them.",
            file=sys.stderr,
        )


def write_with_body_file(
    command: list[str], body: str, *, label: str, prefix: str, original: str | None = None
) -> None:
    """Run a `gh` command with `body` supplied through a UTF-8 file instead of argv.

    newline="" matters as much as the codec. Text mode translates every LF to CRLF on
    Windows, so the body GitHub stored would not be the body that was read, and each cycle
    would add another CR -- the same silent corruption of handoff truth as the locale codec,
    reintroduced by the fix for it (Issue #35).
    """
    guard_lossy_body(label, body, original)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", prefix=prefix, suffix=".md", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(body)
        shell([*command, "--body-file", str(temporary)])
    finally:
        temporary.unlink(missing_ok=True)


def write_issue_body(number: Any, body: str, *, prefix: str, original: str | None = None) -> None:
    write_with_body_file(
        ["gh", "issue", "edit", str(number)],
        body,
        label=f"Issue #{number}",
        prefix=prefix,
        original=original,
    )


def write_control_body(number: Any, updated: str, original: str | None = None) -> None:
    write_issue_body(number, updated, prefix="chatfreept-control-", original=original)


def sync_control(config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    control = find_control_issue(config)
    if not control:
        fail("the pinned control Issue was not found; run ./scripts/setup-github")

    issues, pr_by_issue = open_issues_and_prs()
    rows, release_issues = collect_active(issues, pr_by_issue, control.get("number"))
    managed = render_control_block(config, rows, release_issues)

    issue_json = output(["gh", "issue", "view", str(control["number"]), "--json", "body"])
    if not issue_json:
        fail(f"unable to read control Issue #{control['number']}")
    body = json.loads(issue_json).get("body") or ""
    updated = replace_control_block(body, managed)
    if updated == body:
        print(f"Control Issue #{control['number']} is already current.")
        return
    write_control_body(control["number"], updated, original=body)
    print(f"Control Issue #{control['number']} synchronized.")

def main() -> int:
    use_utf8_streams()
    sync_control()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
