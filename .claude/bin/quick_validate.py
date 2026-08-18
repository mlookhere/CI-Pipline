#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import tomllib

HOOKS = Path(__file__).resolve().parents[1] / "hooks"

# workflow/ is not an importable package, so the path is extended before the import below.
# Same pattern as .claude/bin/capture.py, which needs the same stream fix for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workflow"))

# E402 is suppressed for that reason alone -- the import cannot resolve until sys.path is
# extended above.
from bash_tools import use_utf8_streams  # noqa: E402


def scan_secrets(text: str) -> list[str]:
    """Delegate to the hooks' shared prelude.

    Imported lazily: `common` only resolves once the sibling hooks directory is
    on sys.path, and doing that at module scope would put an import after
    executable statements.
    """
    if str(HOOKS) not in sys.path:
        sys.path.insert(0, str(HOOKS))
    from common import scan_secrets as _scan_secrets

    return _scan_secrets(text)


FOCUSED = re.compile(
    r"(?i)(?:\b(?:describe|it|test)\.(?:only|skip)\b|\b(?:fdescribe|fit|xit|xdescribe)\s*\(|"
    r"@pytest\.mark\.skip|pytest\.skip\s*\(|\bcontinue-on-error\s*:\s*true\b)"
)
WEAKEN = re.compile(r"(?i)(?:eslint-disable|type:\s*ignore|#\s*noqa|coverage:\s*ignore|pragma:\s*no cover)")


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def added_text(path: Path, full_text: str) -> str:
    tracked = run_git("ls-files", "--error-unmatch", "--", str(path)).returncode == 0
    if not tracked:
        return full_text
    added: list[str] = []
    for cached in (False, True):
        args = ["diff"]
        if cached:
            args.append("--cached")
        args.extend(["--unified=0", "--no-color", "--", str(path)])
        result = run_git(*args)
        if result.returncode not in (0, 1):
            continue
        added.extend(
            line[1:]
            for line in result.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
    return "\n".join(added)


def parse_error(path: Path, text: str) -> str | None:
    """Return a parse-error message for structured files, or None when clean."""
    try:
        if path.suffix == ".py":
            ast.parse(text, filename=str(path))
        elif path.suffix == ".json":
            json.loads(text)
        elif path.suffix == ".toml":
            tomllib.loads(text)
    except Exception as exc:
        return f"{path}: parse error: {exc}"
    return None


def scan_one(path: Path) -> tuple[list[str], list[str]]:
    """Scan a single file, returning (failures, warnings)."""
    failures: list[str] = []
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return failures, warnings
    additions = added_text(path, text)

    failures.extend(f"{path}: possible {secret}" for secret in scan_secrets(text))
    if re.search(r"(?m)^(?:<<<<<<<|=======|>>>>>>>)", text):
        failures.append(f"{path}: unresolved conflict marker")
    if FOCUSED.search(additions):
        failures.append(f"{path}: newly added focused/skipped test or continue-on-error marker")
    if WEAKEN.search(additions):
        warnings.append(f"{path}: newly added suppression marker; justify it explicitly")

    problem = parse_error(path, text)
    if problem:
        failures.append(problem)
    return failures, warnings


def main() -> int:
    # First statement, before argparse: this validator prints paths and findings that carry
    # non-ASCII, and on Windows the default stdout codec is cp1252. A UnicodeEncodeError here
    # kills the validator mid-report, so the traceback names an encoding rather than whatever
    # it was in the middle of describing (Issue #7).
    use_utf8_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    failures: list[str] = []
    warnings: list[str] = []

    dirty = run_git("diff", "--check").returncode != 0
    staged = run_git("diff", "--cached", "--check").returncode != 0
    if dirty or staged:
        failures.append("git diff --check failed (whitespace error or conflict marker)")

    for raw in args.paths:
        path = Path(raw)
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        file_failures, file_warnings = scan_one(path)
        failures.extend(file_failures)
        warnings.extend(file_warnings)

    for item in warnings:
        print("warning: " + item)
    for item in failures:
        print("failure: " + item, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
