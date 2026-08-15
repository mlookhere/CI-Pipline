#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CONFIG = ROOT / ".claude-workflow.json"
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
JOB_IF_KEY = re.compile(r"(?m)^    if\s*:(.*)$")
# A two-space job name carrying a value on the same line, i.e. a flow mapping. Valid
# YAML that this line-oriented reader cannot see into, so it is rejected rather than
# silently skipped -- an unreadable job is how a rule stops applying without failing.
INLINE_JOB = re.compile(r"(?m)^  ([A-Za-z0-9_-]+):[ \t]*(?!#)\S")
ALWAYS = re.compile(r"\balways\s*\(\s*\)")
NOT_CANCELLED = re.compile(r"!\s*cancelled\s*\(\s*\)")
CLAUDE_ACTION = "anthropics/claude-code-action@"
DENY_EDIT_TOOLS = '--disallowedTools "Edit,Write,NotebookEdit"'
FORK_GUARD = "head.repo.full_name == github.repository"
SANITIZER = "sanitize_claude_input.py"
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


def job_containing(text: str, needle: str) -> str:
    """The job body holding `needle`, or the whole file when it sits outside any job."""
    for _, body in job_blocks(text):
        if needle in body:
            return body
    return text


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


def strip_comment(line: str) -> str:
    """Drop a trailing `#` comment, leaving quoted `#` alone.

    A comment is not part of the condition GitHub evaluates, so a status function
    written in one must not satisfy a rule about the condition.
    """
    quote = ""
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def job_if(body: str) -> str | None:
    """The job-level `if:` value, folded onto one line with comments removed.

    Continuation lines are gathered until a key returns to job-level indentation, so a
    `>-` or `|` block reads the same as an inline condition.
    """
    match = JOB_IF_KEY.search(body)
    if match is None:
        return None
    parts = [strip_comment(match.group(1)).strip()]
    for line in body[match.end() :].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= 4:
            break
        parts.append(strip_comment(line).strip())
    return " ".join(part for part in parts if part)


def check_claude_job_text(text: str, rel: str) -> list[str]:
    """The advisory review must stay unprivileged, sanitised, and closed to forks.

    Extracted from `check` so each property is reachable from a test rather than only
    from a CI run against the one real file.
    """
    if CLAUDE_ACTION not in text:
        return []
    failures: list[str] = []
    claude_block = job_containing(text, CLAUDE_ACTION)
    if WRITE_PERMISSION.search(claude_block):
        failures.append(f"{rel}: Claude-key job appears to have repository write permission")
    if DENY_EDIT_TOOLS not in claude_block:
        failures.append(f"{rel}: advisory Claude job must explicitly deny edit tools")
    if SANITIZER not in text:
        failures.append(f"{rel}: Claude prompt input must be constructed by a bounded sanitizer")
    if FORK_GUARD not in text:
        failures.append(f"{rel}: Claude review must reject forked pull requests")
    return failures


def check_claude_publisher_gate_text(text: str, rel: str) -> list[str]:
    """Nothing privileged may run off the back of the Claude job unless it succeeded.

    The advisory review is unprivileged by construction: no write permission, edit tools
    denied, forks rejected, prompt input sanitised. The job that *publishes* its output
    is the privileged one -- it carries `pull-requests: write` and comments on the pull
    request -- and the only thing standing between the two is the publisher's condition.

    Two properties, both load-bearing, neither implying the other:

    * It must name the Claude job's success. Drop that conjunct and the publisher runs
      on forked pull requests and on runs whose review produced nothing, because the
      fork guard and the label check live on the Claude job, not on this one.
    * It must exclude a cancelled run with `!cancelled()`. A status check function is
      what removes GitHub's implicit `success()` wrapper and puts the condition back in
      charge, which is what stops a superseded run reporting this job as cancelled
      (Issue #31). `always()` is the wrong function here and is rejected outright:
      GitHub documents `!cancelled()` as the alternative to it precisely because
      `always()` keeps running when the run has been cancelled -- on a write-scoped job
      that means commenting on behalf of a run that was already abandoned.

    Structure this reader cannot follow is a failure, not a pass. A flow-mapping job or
    a job name at the wrong indentation would otherwise take the publisher out of scope
    silently, which is the same class of bug as the one being fixed.
    """
    if CLAUDE_ACTION not in text:
        return []
    failures: list[str] = []
    offset = jobs_offset(text)
    for match in INLINE_JOB.finditer(text[offset:]):
        failures.append(
            f"{rel}:{line_of(text, offset + match.start())}: job {match.group(1)!r} is written as "
            "a flow mapping, which this policy reader cannot inspect; declare it as a block "
            "mapping so its permissions and condition stay checkable"
        )
    blocks = job_blocks(text)
    keyed = [name for name, body in blocks if CLAUDE_ACTION in body]
    if not keyed:
        failures.append(
            f"{rel}: this workflow runs the Claude action but no job block containing it could be "
            "located; job names are expected at two-space indentation under `jobs:`"
        )
        return failures
    required = [re.compile(rf"needs\.{re.escape(name)}\.result\s*==\s*['\"]success['\"]") for name in keyed]
    for name, body in blocks:
        if CLAUDE_ACTION in body or not WRITE_PERMISSION.search(body):
            continue
        condition = job_if(body) or ""
        if not any(pattern.search(condition) for pattern in required):
            failures.append(
                f"{rel}: job {name!r} has repository write permission in a workflow that runs the "
                f"Claude action, but its condition does not require {keyed[0]!r} to have "
                "succeeded; a privileged publisher must gate on the advisory job's success "
                "explicitly, because that is what keeps it off forked pull requests"
            )
        if ALWAYS.search(condition):
            failures.append(
                f"{rel}: job {name!r} has repository write permission and uses always(), which "
                "keeps running after the run is cancelled; use !cancelled() instead"
            )
        elif not NOT_CANCELLED.search(condition):
            failures.append(
                f"{rel}: job {name!r} has repository write permission but its condition carries no "
                "status check function, so GitHub's implicit success() wrapper decides it and a "
                "superseded run reports it cancelled rather than skipped; add !cancelled()"
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
    failures.extend(check_claude_job_text(text, rel))
    failures.extend(check_closure_concurrency_text(text, rel))
    failures.extend(check_claude_publisher_gate_text(text, rel))
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
