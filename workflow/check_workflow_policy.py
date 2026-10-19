#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CONFIG = ROOT / ".chatfreept" / "github.json"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

RULES = [
    (re.compile(r"(?im)^\s*permissions\s*:\s*write-all\s*$"), "workflow grants write-all permissions"),
    (re.compile(r"(?im)^\s*persist-credentials\s*:\s*true\s*$"), "checkout persists repository credentials"),
    (
        re.compile(r"(?im)^\s*prompt\s*:\s*.*\$\{\{\s*github\.event\b"),
        "untrusted event data is interpolated directly into an agent prompt",
    ),
    (
        re.compile(r"(?i)\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b"),
        "remote content is piped directly into a shell",
    ),
]

UNTRUSTED_HEAD = re.compile(
    r"(?i)(?:github\.event\.pull_request\.head(?:\.sha|\.ref|\.repo)?|github\.head_ref|gh\s+pr\s+checkout)"
)

CLOSURE_HANDLER = "handle_pr_state.py"
JOBS_KEY = re.compile(r"(?m)^jobs\s*:\s*$")
JOB_HEADING = re.compile(r"(?m)^  ([A-Za-z0-9_-]+):\s*$")
CONCURRENCY_KEY = re.compile(r"(?im)^([ \t]*)concurrency\s*:(.*)$")
# Four spaces exactly: job-level keys, so a step's own `if:` cannot pose as the job's.
# A two-space job name carrying a value on the same line, i.e. a flow mapping. Valid
# YAML that this line-oriented reader cannot see into, so it is rejected rather than
# silently skipped -- an unreadable job is how a rule stops applying without failing.
# A program handed to an interpreter on stdin: a heredoc, or a lone `-` argument. Code that
# arrives that way is invisible to every gate this repository runs -- it is not compiled by
# the fast gate, not linted, not type-checked, and no test can reach it -- which is how the
# advisory review comment came to be rendered by an unvalidated one-liner (Issue #41).
STDIN_PROGRAM = re.compile(r"<<-?\s*[\"']?[A-Za-z_]\w*|\b(?:python3?|node|ruby|perl|bash|sh|zsh)\s+-(?=\s|$)")
WRITE_PERMISSION = re.compile(r"(?im)^\s*(?:contents|pull-requests|issues|actions|checks)\s*:\s*write\s*$")
# Line-anchored so a `${{ ... }}` group value keeps its closing braces.
GROUP_SETTING = re.compile(r"(?im)^\s*group\s*:\s*(.+?)\s*$")
CANCEL_SETTING = re.compile(r"(?im)^\s*cancel-in-progress\s*:\s*(.+?)\s*$")


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def jobs_offset(text: str) -> int:
    match = JOBS_KEY.search(text)
    return match.start() if match else len(text)


def job_blocks(text: str) -> list[tuple[str, str]]:
    """`(name, body)` per job. Sliced inside `jobs:` so `on:` sub-keys cannot pose as jobs."""
    section = text[jobs_offset(text) :]
    heads = list(JOB_HEADING.finditer(section))
    blocks = []
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(section)
        blocks.append((head.group(1), section[head.start() : end]))
    return blocks


def concurrency_block(text: str) -> str:
    """The `concurrency:` mapping, normalised so block and inline forms read alike."""
    match = CONCURRENCY_KEY.search(text)
    if not match:
        return ""
    inline = match.group(2).strip()
    if inline:
        return "\n".join(part.strip() for part in inline.strip("{} ").split(","))
    indent = len(match.group(1))
    lines: list[str] = []
    for line in text[match.end() :].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        lines.append(line)
    return "\n".join(lines)


def cancels_in_progress(block: str) -> bool:
    """Anything but an explicit `false` cancels.

    An expression such as `${{ github.event_name != 'push' }}` is not provably safe from
    here, so it is read as cancelling rather than waved through.
    """
    match = CANCEL_SETTING.search(block)
    if match is None:
        return False
    return unquote(match.group(1)).lower() != "false"


def group_of(block: str) -> str | None:
    match = GROUP_SETTING.search(block)
    return unquote(match.group(1)) if match else None


def check_closure_concurrency_text(text: str, rel: str) -> list[str]:
    """The one-shot pull-request-closure handler must not sit in a cancelling group.

    `handle_pr_state.py` moves a merged pull request's controlling Issue to
    release-ready. It answers a `closed` event that will never be re-delivered, so a
    cancellation loses the transition permanently -- and a cancelled run is
    indistinguishable from the benign cancellations a burst of pushes produces, so it
    fails invisibly. The idempotent sync alongside it recomputes state from scratch and
    may keep `cancel-in-progress`; this handler may not (Issue #36).

    Three ways to be cancellable, all rejected: a workflow-level cancelling group, which
    covers every trigger; a cancelling group on the handler's own job; and a group the
    handler shares with a cancelling job, since `cancel-in-progress` is a property of the
    *incoming* run -- the other job's runs would cancel this one however this job is
    configured.
    """
    if CLOSURE_HANDLER not in text:
        return []
    failures: list[str] = []
    header = text[: jobs_offset(text)]
    declared = CONCURRENCY_KEY.search(header)
    if declared and cancels_in_progress(concurrency_block(header)):
        failures.append(
            f"{rel}:{line_of(text, declared.start())}: a workflow-level cancel-in-progress group "
            f"covers every trigger, so any of them can cancel the one-shot {CLOSURE_HANDLER} run; "
            "give each job the group its own idempotence justifies"
        )
    blocks = job_blocks(text)
    for name, body in blocks:
        if CLOSURE_HANDLER not in body:
            continue
        own = concurrency_block(body)
        if cancels_in_progress(own):
            failures.append(
                f"{rel}: job {name!r} runs {CLOSURE_HANDLER} under cancel-in-progress; "
                "a one-shot closure handler must not be cancellable"
            )
        group = group_of(own)
        if group is None:
            continue
        for other, other_body in blocks:
            if other == name:
                continue
            shared = concurrency_block(other_body)
            if group_of(shared) == group and cancels_in_progress(shared):
                failures.append(
                    f"{rel}: job {name!r} runs {CLOSURE_HANDLER} in concurrency group {group!r}, "
                    f"which job {other!r} shares with cancel-in-progress; that job's runs would "
                    "cancel this one"
                )
    return failures


# A checked-in Actions workflow must never bypass the repository's protected gates.
# Dynamic GitHub MCP operations are governed by GitHub permissions and explicit CI proof.
BYPASS_COMMAND = re.compile(
    r"(?i)(?:\bgh\s+pr\s+merge\b[^\n#]*--admin\b"
    r"|\bgit\s+push\b[^\n#]*(?:--force(?:-with-lease)?|-f\b)"
    r"|\bgit\s+(?:commit|push)\b[^\n#]*--no-verify\b"
    r"|\bgit\s+reset\s+--hard\b"
    r"|\bgh\s+(?:secret|variable)\s+(?:set|delete|remove)\b)"
)


def check_no_gate_bypass_text(text: str, rel: str) -> list[str]:
    """Reject protected-branch bypasses in executable workflow lines, not YAML comments."""
    failures = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if BYPASS_COMMAND.search(line):
            failures.append(f"{rel}:{number}: protected CI gate bypass command is forbidden")
    return failures


EXTERNAL_AGENT = re.compile(
    r"(?i)(?:anthropics/claude-code-action|ANTHROPIC_API_KEY|claude:review|scripts/claude-exec)"
)


def check_no_external_agent_text(text: str, rel: str) -> list[str]:
    """No secondary AI agent, API key or review trigger may enter workflow execution."""
    return (
        [f"{rel}: secondary autonomous agent dependency is forbidden"] if EXTERNAL_AGENT.search(text) else []
    )


def check_privileged_inline_script_text(text: str, rel: str) -> list[str]:
    """A job holding write permission must not run a program fed to an interpreter on stdin.

    Nothing else in this repository can see such a program. `self_test.py` compiles the
    tracked Python, ruff lints it, mypy checks it and pytest exercises it -- all of them by
    path, none of them inside a YAML string. A heredoc is therefore the one place where
    privileged logic can be written with no gate reading it, which is exactly where the
    advisory review's unvalidated renderer survived (Issue #41).

    Scoped to write permission rather than applied to every job, because the cost of being
    unreviewable scales with what the job may do: an inline script in a read-only job can
    only mislead its own log, while this one comments as the repository.
    """
    failures: list[str] = []
    for name, body in job_blocks(text):
        if not WRITE_PERMISSION.search(body):
            continue
        found = STDIN_PROGRAM.search(body)
        if found:
            failures.append(
                f"{rel}: job {name!r} has repository write permission and feeds a program to an "
                f"interpreter on stdin ({found.group(0).strip()!r}); code written there is not "
                "compiled, linted, type-checked or tested by any gate here, so put it under "
                "workflow/ and call it by path"
            )
    return failures


def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    failures: list[str] = []
    for pattern, message in RULES:
        for match in pattern.finditer(text):
            failures.append(f"{path.relative_to(ROOT)}:{line_of(text, match.start())}: {message}")

    if re.search(r"(?m)^\s*pull_request_target\s*:", text):
        for match in UNTRUSTED_HEAD.finditer(text):
            failures.append(
                f"{path.relative_to(ROOT)}:{line_of(text, match.start())}: "
                "pull_request_target workflow references an untrusted PR head; "
                "check out only a protected base/default branch"
            )
        if re.search(r"(?im)^\s*contents\s*:\s*write\s*$", text):
            failures.append(
                f"{path.relative_to(ROOT)}: pull_request_target workflow must not have contents: write"
            )

    rel = str(path.relative_to(ROOT))
    failures.extend(check_no_external_agent_text(text, rel))
    failures.extend(check_no_gate_bypass_text(text, rel))
    failures.extend(check_closure_concurrency_text(text, rel))
    failures.extend(check_privileged_inline_script_text(text, rel))
    return failures


def integration_branch() -> str:
    try:
        return str(json.loads(CONFIG.read_text(encoding="utf-8"))["branches"]["integration"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return "dev"


def check_dependabot_text(text: str, branch: str, rel: str = "dependabot.yml") -> list[str]:
    """Every Dependabot ecosystem must target the integration branch.

    With no target-branch, Dependabot opens pull requests against the repository
    default branch -- the production branch here. ci-pr.yml only triggers on
    pull requests into the integration branch, so those PRs skip the PR gate
    entirely and would carry dependency changes onto production without ever
    passing through integration.
    """
    starts = [match.start() for match in re.finditer(r"(?m)^\s*-\s*package-ecosystem\s*:", text)]
    failures: list[str] = []
    for index, start in enumerate(starts):
        block = text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        named = re.search(r"package-ecosystem\s*:\s*[\"']?([^\"'\s#]+)", block)
        name = named.group(1) if named else "unknown"
        target = re.search(r"(?m)^\s*target-branch\s*:\s*[\"']?([^\"'\s#]+)", block)
        if target is None:
            failures.append(
                f"{rel}:{line_of(text, start)}: dependabot ecosystem {name!r} sets no target-branch, "
                f"so its pull requests would bypass the PR gate; set target-branch: {branch!r}"
            )
        elif target.group(1) != branch:
            failures.append(
                f"{rel}:{line_of(text, start + target.start())}: dependabot ecosystem {name!r} targets "
                f"{target.group(1)!r}; expected the integration branch {branch!r}"
            )
    return failures


def check_dependabot() -> list[str]:
    if not DEPENDABOT.is_file():
        return []
    return check_dependabot_text(
        DEPENDABOT.read_text(encoding="utf-8"),
        integration_branch(),
        str(DEPENDABOT.relative_to(ROOT)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject high-risk GitHub Actions patterns.")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    paths = [Path(value) for value in args.paths] if args.paths else sorted(WORKFLOWS.glob("*.y*ml"))
    failures: list[str] = check_dependabot()
    for path in paths:
        if path.is_file():
            failures.extend(check(path.resolve()))
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        print(f"Workflow policy failed with {len(failures)} finding(s).")
        return 1
    print(f"Workflow policy passed for {len(paths)} workflow file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
