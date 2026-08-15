#!/usr/bin/env python3
"""Report the type findings the blocking gate cannot see, without blocking on them.

`pyproject.toml` sets `no_site_packages = true` so the fast gate stays reproducible:
numpy >= 2.3 ships PEP 695 stubs that are a fatal parse error under `python_version =
3.10`, and mypy abandons the whole run. The cost, recorded in that file's own comment, is
that our code is then checked against no third-party types at all -- so any finding that
depends on chromadb's, fastapi's or pydantic's signatures is invisible every day.

This runs the same code with those types available and prints what it finds. It is
advisory **by construction** rather than by configuration: it exits 0 whatever mypy says.

That is deliberate, and it is not `|| true`. A shell fallback would make `ci/run.py` record
a clean run it never had, losing the count and the findings together. Here the findings are
printed, written to the stage artifact, and surfaced as GitHub annotations; only the exit
code is suppressed, and this docstring is why.

Blocking on these would be wrong: a new release of a third-party package can add or remove
findings without a line of this repository changing, and a gate that goes red for that
teaches people to ignore it -- which is exactly the decay Issue #24 exists to prevent.
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
# `path:line: error: message  [code]`
FINDING = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): error: (?P<message>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")


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


def main() -> int:
    if not CONFIG.is_file():
        print(f"typed advisory: {CONFIG} is missing; nothing to report")
        return 0
    interpreter = sys.argv[1] if len(sys.argv) > 1 else sys.executable
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
            # nothing and this reported zero findings against a non-zero exit. The
            # self-check below caught that; the flag stops it recurring.
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
    for finding in findings:
        # A GitHub annotation, so a reader sees these on the pull request rather than
        # only inside a log nobody opens.
        print(f"::notice file={finding['path']},line={finding['line']}::{finding['message']}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({"count": len(findings), "findings": findings}, indent=2), encoding="utf-8")

    print(f"\ntyped advisory: {len(findings)} finding(s) reported, none blocking. See {REPORT.name}.")
    if completed.returncode != 0 and not findings:
        # mypy failed without producing findings -- a crash or a bad config. That is worth
        # saying out loud, because a reporter that silently reports nothing is the failure
        # mode this file exists to avoid.
        print("typed advisory: mypy exited non-zero without findings; the check itself is broken")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
