#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF_PATH = "ci/quality.py"
CONFIG = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
QUALITY = CONFIG.get("quality", {})

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}

DEBUG_PATTERNS = {
    ".js": [r"\bdebugger\s*;", r"\bconsole\.log\s*\("],
    ".jsx": [r"\bdebugger\s*;", r"\bconsole\.log\s*\("],
    ".ts": [r"\bdebugger\s*;", r"\bconsole\.log\s*\("],
    ".tsx": [r"\bdebugger\s*;", r"\bconsole\.log\s*\("],
    ".py": [r"\bbreakpoint\s*\(", r"\bpdb\.set_trace\s*\("],
    ".rb": [r"\bbinding\.pry\b", r"\bbyebug\b"],
}


class ScopeError(RuntimeError):
    """The set of files to inspect could not be determined."""


def git_lines(*args: str) -> list[str]:
    """Return git output lines, raising when git itself fails.

    This previously returned [] on any git error, which made "nothing changed"
    and "git could not answer" indistinguishable: an unresolvable base ref
    produced an empty scope and the gate reported success having read nothing.
    """
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise ScopeError(
            f"`git {' '.join(args)}` failed with exit {result.returncode}: "
            + (detail[-1] if detail else "no error output")
        )
    return [line for line in result.stdout.splitlines() if line]


def ref_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def excluded(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in QUALITY.get("exclude_globs", []))


def resolve_base() -> str | None:
    """The ref to diff against, or None when HEAD has no parent.

    Substitutions are announced rather than applied silently. A configured base
    that does not resolve -- a shallow CI checkout, say -- otherwise narrows the
    scope to the last commit instead of the whole branch, and nothing says so.
    """
    base = os.environ.get("CI_BASE_REF") or QUALITY.get("base_ref") or "origin/dev"
    if ref_exists(base):
        return base
    if ref_exists("HEAD~1"):
        print(
            f"warning: base ref {base!r} does not resolve; falling back to HEAD~1, "
            "so this run inspects only the last commit",
            file=sys.stderr,
        )
        return "HEAD~1"
    print(
        f"warning: base ref {base!r} does not resolve and HEAD has no parent; "
        "treating every tracked file as in scope",
        file=sys.stderr,
    )
    return None


def files_for_mode(mode: str) -> tuple[list[str], str]:
    """Return the files to inspect and a description of what they were compared against."""
    if mode == "staged":
        files = git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMR")
        against = "the index"
    else:
        base = resolve_base()
        if base is None:
            files = git_lines("ls-files")
            against = "all tracked files (HEAD has no parent)"
        else:
            files = git_lines("diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD")
            against = base
        # Staged edits appear in neither the committed diff nor the unstaged diff,
        # so both are needed: otherwise `./ci/run fast` run after `git add` but
        # before `git commit` inspects nothing at all.
        files.extend(git_lines("diff", "--cached", "--name-only", "--diff-filter=ACMR"))
        files.extend(git_lines("diff", "--name-only", "--diff-filter=ACMR"))
    return sorted({path for path in files if not excluded(path) and (ROOT / path).is_file()}), against


def readable_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192] or len(data) > 4_000_000:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


TODO_WITHOUT_ISSUE = re.compile(r"\b(?:TODO|FIXME)\b(?![^\n]{0,80}(?:#\d+|https://github\.com/))")


class Limits:
    """Quality thresholds resolved once from .claude-workflow.json."""

    def __init__(self) -> None:
        self.max_lines = int(QUALITY.get("max_changed_file_lines", 0) or 0)
        self.source_extensions = set(QUALITY.get("source_extensions", []))
        self.text_extensions = set(QUALITY.get("text_extensions", []))
        self.banned = [phrase.lower() for phrase in QUALITY.get("banned_phrases", [])]
        self.require_issue_for_todo = bool(QUALITY.get("require_issue_for_todo", False))


def scan_size(relative: str, suffix: str, text: str, limits: Limits) -> list[str]:
    if not limits.max_lines or suffix not in limits.source_extensions:
        return []
    count = text.count("\n") + 1
    if count <= limits.max_lines:
        return []
    return [f"{relative}: file has {count} lines; limit is {limits.max_lines}"]


def scan_patterns(relative: str, suffix: str, text: str) -> list[str]:
    found = []
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            found.append(f"{relative}:{line_number(text, match.start())}: possible {name}")
    for pattern_text in DEBUG_PATTERNS.get(suffix, []):
        for match in re.finditer(pattern_text, text):
            found.append(
                f"{relative}:{line_number(text, match.start())}: debug statement matches {pattern_text}"
            )
    return found


def scan_prose(relative: str, suffix: str, text: str, limits: Limits) -> list[str]:
    found = []
    if suffix in limits.text_extensions:
        lower = text.lower()
        for phrase in limits.banned:
            start = 0
            while (index := lower.find(phrase, start)) >= 0:
                found.append(f"{relative}:{line_number(text, index)}: banned phrase {phrase!r}")
                start = index + len(phrase)
    if limits.require_issue_for_todo:
        for match in TODO_WITHOUT_ISSUE.finditer(text):
            found.append(
                f"{relative}:{line_number(text, match.start())}: TODO/FIXME lacks an Issue reference"
            )
    return found


def scan_file(relative: str, path: Path, text: str, limits: Limits) -> list[str]:
    suffix = path.suffix.lower()
    findings = scan_size(relative, suffix, text, limits)
    findings += scan_patterns(relative, suffix, text)
    # This module is the only file that must contain every marker it searches
    # for -- the rule definitions and their operator-facing messages are the
    # patterns, so scanning it would make the gate permanently fail on its own
    # source. Narrow by design: one path, not a general suppression pragma that
    # could hide real markers elsewhere.
    if relative != SELF_PATH:
        findings += scan_prose(relative, suffix, text, limits)
    return findings


BROKEN_TOOL = re.compile(r"^(?:ModuleNotFoundError|ImportError|Traceback \(most recent call last\))", re.M)


def run_tool(command: list[str], name: str, finding: str) -> str:
    """Run an external gate tool, separating "tool is broken" from "tool found a problem".

    A crashed scanner must never be reported as a content finding: "detect-secrets
    reported a secret" and "detect-secrets could not start" demand opposite
    responses from whoever reads the gate output. Both still fail the gate.
    """
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        print(output, end="" if output.endswith("\n") else "\n")
    if result.returncode == 0:
        return ""
    if BROKEN_TOOL.search(output):
        return (
            f"{name} failed to execute (broken or incomplete install, not a code finding); "
            "reinstall with ./scripts/bootstrap"
        )
    if result.returncode < 0 or result.returncode > 125:
        return f"{name} terminated abnormally with exit code {result.returncode}; treat as inconclusive"
    return f"{name} {finding}"


def resolve_scope(mode: str) -> tuple[list[str], str] | None:
    """Print the scope line. None means the scope could not be determined."""
    try:
        files, against = files_for_mode(mode)
    except ScopeError as exc:
        print(f"Quality scope ({mode}): UNDETERMINED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        print("refusing to report success for a scope that could not be read", file=sys.stderr)
        return None
    print(f"Quality scope ({mode}): {len(files)} file(s) vs {against}")
    return files, against


def scan_all(files: list[str], limits: Limits) -> tuple[list[str], list[str]]:
    """Scan every readable file, returning its findings and the source files among them."""
    failures: list[str] = []
    source_files: list[str] = []
    for relative in files:
        path = ROOT / relative
        if path.suffix.lower() in limits.source_extensions:
            source_files.append(relative)
        text = readable_text(path)
        if text is not None:
            failures.extend(scan_file(relative, path, text, limits))
    return failures, source_files


def external_tool_findings(files: list[str], source_files: list[str]) -> list[str]:
    """Run lizard and detect-secrets. A missing tool is a failure, never a silent skip."""
    findings: list[str] = []
    lizard = shutil.which("lizard")
    if source_files and lizard:
        command = [
            lizard,
            "-w",
            "-C",
            str(QUALITY.get("max_cyclomatic_complexity", 15)),
            "-L",
            str(QUALITY.get("max_function_lines", 120)),
            *source_files,
        ]
        print("$ " + " ".join(command))
        findings.append(run_tool(command, "lizard", "complexity/function-length thresholds failed"))
    elif source_files:
        findings.append("lizard is unavailable; run ./scripts/bootstrap before quality checks")

    detect_secrets = shutil.which("detect-secrets-hook")
    if files and detect_secrets:
        print("$ detect-secrets-hook <changed files>")
        findings.append(
            run_tool([detect_secrets, *files], "detect-secrets", "reported one or more possible secrets")
        )
    elif files:
        findings.append("detect-secrets-hook is unavailable; run ./scripts/bootstrap before quality checks")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["staged", "changed"], default="changed")
    args = parser.parse_args()
    scope = resolve_scope(args.mode)
    if scope is None:
        return 1
    files, _ = scope
    if not files:
        print("Quality checks passed (nothing in scope).")
        return 0

    limits = Limits()
    failures, source_files = scan_all(files, limits)
    failures.extend(external_tool_findings(files, source_files))
    failures = [failure for failure in failures if failure]

    if failures:
        print("\nQuality failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Quality checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
