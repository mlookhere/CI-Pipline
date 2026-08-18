#!/usr/bin/env python3
"""Report the type findings the blocking gate cannot see, without blocking on them.

`pyproject.toml` sets `no_site_packages = true` so the fast gate stays reproducible:
numpy >= 2.3 ships PEP 695 stubs that are a fatal parse error under `python_version =
3.10`, and mypy abandons the whole run. The cost, recorded in that file's own comment, is
that our code is then checked against no third-party types at all -- so any finding that
depends on chromadb's, fastapi's or pydantic's signatures is invisible every day.

This runs the same code with those types available and prints what it finds.

**Findings do not fail this check. A broken check does.** Exiting non-zero on findings
would be wrong: a third-party release can add or remove them without a line of this
repository changing, and a gate that reddens for that teaches people to ignore it. But
every way this script can report nothing -- the wrong interpreter, a missing config, a
crashed mypy, a parser that matches nothing -- is a defect *here*, and reporting silence
as safety is the failure this file exists to prevent. It has already happened twice:
running under the toolchain venv, which has no runtime packages, and a parser defeated by
ANSI colour. So the checker's own health is checked, and only that fails the job.

Not `|| true` either, which would make `ci/run.py` record a clean run it never had.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "ci" / "mypy-advisory.ini"
REPORT = ROOT / "artifacts" / "ci" / "typed-advisory-findings.json"
# `path:line: error: message  [code]`. The path is matched non-greedily rather than as
# "anything but a colon": a Windows absolute path carries a drive colon, and `[^:]+` could
# not cross it, so every finding was dropped silently whenever mypy reported absolute
# paths. Relative paths from `cwd=ROOT` hid that.
FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+): error: (?P<message>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")


def witness() -> str:
    """A package present only when the interpreter carries the runtime dependencies.

    Its absence means this check would report on stub-free code and call that success, so the
    witness exists to make that silence loud. Which package plays the part is the consumer's
    to say -- it was `chromadb` here, written into a module a control plane means to hand to
    other repositories (Issue #93). A consumer with no runtime dependencies declares none and
    the check skips, saying so.
    """
    try:
        config = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(config.get("project", {}).get("typed_advisory_witness", ""))


def parse(output: str) -> list[dict[str, str]]:
    findings = []
    for line in output.splitlines():
        match = FINDING.match(line.strip())
        if match:
            findings.append(
                {
                    "path": match.group("path").replace("\\", "/"),
                    "line": match.group("line"),
                    "code": match.group("code") or "",
                    "message": match.group("message"),
                }
            )
    return findings


def sees_runtime_types(interpreter: str) -> bool:
    """Whether this interpreter actually carries the dependencies being checked against.

    `ci/run.py` falls back to its own interpreter when the project virtualenv is missing,
    so a half-finished bootstrap would run mypy against the toolchain venv -- no
    third-party types, no findings, green forever, which is precisely the regression this
    check is supposed to make impossible.
    """
    name = witness()
    if not name:
        return False
    try:
        completed = subprocess.run(
            [interpreter, "-c", f"import {name}"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def write(findings: list[dict[str, str]], note: str) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps({"count": len(findings), "note": note, "findings": findings}, indent=2),
        encoding="utf-8",
    )


def broken(reason: str) -> int:
    """The checker itself failed. Say so loudly and fail, so it cannot rot green."""
    write([], reason)
    print(f"::error::typed advisory is not working: {reason}")
    print(f"typed advisory: NOT RUN -- {reason}")
    return 1


def main() -> int:
    interpreter = sys.argv[1] if len(sys.argv) > 1 else ""
    if not CONFIG.is_file():
        return broken(f"{CONFIG.name} is missing, so there is no configuration to check with")
    if not interpreter:
        return broken("no project interpreter was given, so no third-party types would be seen")
    # Deliberately not "is this a different interpreter from mine". That was tried and it
    # is wrong twice over: on Linux a virtualenv's `python` is a symlink to the same base
    # interpreter, so `Path.resolve()` makes two distinct environments compare equal and
    # the check fired on every CI run; and identity was only ever a proxy for the thing
    # that matters, which is whether the runtime types are actually visible. Ask that.
    if not witness():
        return broken(
            "no project.typed_advisory_witness is configured, so there is nothing to prove "
            "the interpreter carries runtime types and a silent result cannot be trusted"
        )
    if not sees_runtime_types(interpreter):
        return broken(
            f"{interpreter} cannot import {witness()}, so mypy would check against no "
            "third-party types and report success regardless"
        )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(CONFIG),
            "--python-executable",
            interpreter,
            # Colour codes sit between the line start and the path, so the parser matched
            # nothing and this reported zero findings against a non-zero exit.
            "--no-color-output",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = completed.stdout + completed.stderr
    findings = parse(output)
    print(output.rstrip())

    if completed.returncode != 0 and not findings:
        # mypy failed and produced nothing this parser recognised: a crash, a bad config,
        # or an output format that has moved. Either way the report would be a lie.
        return broken("mypy exited non-zero without any finding this parser recognised")

    for finding in findings:
        # A GitHub annotation, so a reader sees these on the pull request rather than only
        # inside a log nobody opens.
        print(f"::notice file={finding['path']},line={finding['line']}::{finding['message']}")

    write(findings, "advisory only; findings do not fail this check")
    print(f"\ntyped advisory: {len(findings)} finding(s) reported, none blocking. See {REPORT.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
