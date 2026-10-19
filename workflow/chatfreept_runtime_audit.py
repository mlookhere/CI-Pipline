"""Fail-closed audit of shipped, executable ChatGPT CI assets (#102).

No network calls; inspects the repository and the exact vendoring allowlist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from pipeline_sync import portable_files

ROOT = Path(__file__).resolve().parents[1]

LEGACY_PATHS = (
    ".claude",
    ".claude-workflow.json",
    "CLAUDE.md",
    "flow",
    ".gitignore.claude-ci",
    ".github/claude",
    ".github/workflows/claude-review.yml",
    "scripts/claude-exec",
    "scripts/claude-lease",
    "workflow/claude_flow.py",
    "workflow/claude_lease.py",
    "workflow/claude_usage_report.py",
    "workflow/render_claude_review.py",
    "workflow/sanitize_claude_input.py",
)
RUNTIME_DIRS = ("ci", "scripts", "workflow", ".github/workflows")
ACTION = re.compile(r"(?im)^\s*(?:-\s*)?uses\s*:\s*anthropics\s*/\s*claude", re.I)
EXECUTION = re.compile(
    r"""(?im)^\s*(?:run\s*:\s*|exec\s+|os\.execvp\s*\(|subprocess\.(?:run|Popen)\s*\()"""
    r""".*(?:["']claude["']|\bclaude\s+(?:--|\S+)|ANTHROPIC_API_KEY)"""
)
CONFIGURATION = re.compile(
    r"""(?im)^\s*(?:CONFIG|CONFIG_PATH|LEGACY_PATH)\s*=.*["']\.claude-workflow\.json["']"""
)
SHELL_RUNTIME = re.compile(r"(?m)^\s*(?!#).*(?:CLAUDE_CI_HOME|CLAUDE_CI_PYTHON|CLAUDE_BASH)")
MAX_BYTES = 250_000


def runtime_sources(root: Path) -> Iterable[tuple[str, Path]]:
    for folder in RUNTIME_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                yield path.relative_to(root).as_posix(), path


def forbidden_text(name: str, text: str) -> list[str]:
    patterns = (ACTION, EXECUTION, CONFIGURATION)
    if name.startswith("scripts/") or name.startswith("ci/"):
        patterns = (*patterns, SHELL_RUNTIME)
    return [f"{name}: forbidden agent runtime reference" for pattern in patterns if pattern.search(text)]


def verify(root: Path = ROOT) -> list[str]:
    failures = [
        f"legacy runtime path is still present: {name}" for name in LEGACY_PATHS if (root / name).exists()
    ]
    try:
        vendored = portable_files(root)
    except (OSError, UnicodeError, ValueError) as exc:
        return [*failures, f"unable to inspect vendor manifest: {exc}"]
    if not vendored or "workflow/chatfreept_state.py" not in vendored:
        failures.append("runtime-neutral vendor manifest is missing core ChatGPT state")
    for name in vendored:
        if "claude" in name.lower() or "anthropic" in name.lower():
            failures.append(f"forbidden vendor path: {name}")
    for name, path in runtime_sources(root):
        if path.stat().st_size > MAX_BYTES:
            failures.append(f"runtime source exceeds audit read limit: {name}")
            continue
        try:
            failures.extend(forbidden_text(name, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError) as exc:
            failures.append(f"runtime source unreadable: {name}: {exc}")
    return failures


def main() -> int:
    failures = verify()
    for finding in failures:
        print(f"failure: {finding}")
    if failures:
        print(f"ChatGPT runtime audit failed with {len(failures)} finding(s).")
        return 1
    print("ChatGPT runtime audit passed (including owned vendor files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
