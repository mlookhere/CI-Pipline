#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

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


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


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

    if "anthropics/claude-code-action@" in text:
        action_offset = text.index("anthropics/claude-code-action@")
        job_starts = list(re.finditer(r"(?m)^  [A-Za-z0-9_-]+:\s*$", text))
        start = max((m.start() for m in job_starts if m.start() < action_offset), default=0)
        end = min((m.start() for m in job_starts if m.start() > action_offset), default=len(text))
        job_block = text[start:end]
        if re.search(
            r"(?im)^\s*(?:contents|pull-requests|issues|actions|checks)\s*:\s*write\s*$",
            job_block,
        ):
            failures.append(
                f"{path.relative_to(ROOT)}: Claude-key job appears to have repository write permission"
            )
        if '--disallowedTools "Edit,Write,NotebookEdit"' not in job_block:
            failures.append(f"{path.relative_to(ROOT)}: advisory Claude job must explicitly deny edit tools")
        if "sanitize_claude_input.py" not in text:
            failures.append(
                f"{path.relative_to(ROOT)}: Claude prompt input must be constructed by a bounded sanitizer"
            )
        if "head.repo.full_name == github.repository" not in text:
            failures.append(f"{path.relative_to(ROOT)}: Claude review must reject forked pull requests")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject high-risk GitHub Actions patterns.")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    paths = [Path(value) for value in args.paths] if args.paths else sorted(WORKFLOWS.glob("*.y*ml"))
    failures: list[str] = []
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
