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


def run_git(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def excluded(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in QUALITY.get("exclude_globs", []))


def files_for_mode(mode: str) -> list[str]:
    if mode == "staged":
        files = run_git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        base = os.environ.get("CI_BASE_REF") or QUALITY.get("base_ref") or "origin/dev"
        if (
            subprocess.run(
                ["git", "rev-parse", "--verify", base],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            files = run_git("diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD")
        else:
            files = run_git("diff", "--name-only", "--diff-filter=ACMR", "HEAD~1...HEAD")
        files.extend(run_git("diff", "--name-only", "--diff-filter=ACMR"))
    return sorted({path for path in files if not excluded(path) and (ROOT / path).is_file()})


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["staged", "changed"], default="changed")
    args = parser.parse_args()
    files = files_for_mode(args.mode)
    print(f"Quality scope ({args.mode}): {len(files)} file(s)")
    if not files:
        return 0

    failures: list[str] = []
    source_files: list[str] = []
    limits = Limits()
    source_extensions = limits.source_extensions

    for relative in files:
        path = ROOT / relative
        if path.suffix.lower() in source_extensions:
            source_files.append(relative)
        text = readable_text(path)
        if text is None:
            continue
        failures.extend(scan_file(relative, path, text, limits))

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
        failures.append(run_tool(command, "lizard", "complexity/function-length thresholds failed"))
    elif source_files:
        failures.append("lizard is unavailable; run ./scripts/bootstrap before quality checks")

    detect_secrets = shutil.which("detect-secrets-hook")
    if files and detect_secrets:
        command = [detect_secrets, *files]
        print("$ detect-secrets-hook <changed files>")
        failures.append(run_tool(command, "detect-secrets", "reported one or more possible secrets"))
    elif files:
        failures.append("detect-secrets-hook is unavailable; run ./scripts/bootstrap before quality checks")

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
