#!/usr/bin/env python3
"""Keep runtime dependencies declared in exactly one place.

`requirements.txt` and `[project].dependencies` used to hold the same nine lines. Nothing
compared them, and only one of them was ever audited -- `pip-audit` reads
`requirements.txt` -- so a dependency added to `pyproject.toml` alone would ship without
ever being scanned (Issue #16).

The fix is delegation rather than comparison: `pyproject.toml` declares its dependencies
`dynamic` and reads `requirements.txt`, so the two cannot disagree. This module guards the
arrangement itself, because the way it would break is not a mismatch appearing -- it is
someone re-adding a static list, at which point the drift is back and nothing says so.

Deliberately line-oriented rather than parsed. `requires-python` is `>=3.10`, `tomllib`
arrived in 3.11, and the fast gate runs under whichever interpreter `scripts/bootstrap`
resolved. The facts being checked are exact declarations in files this repository owns,
not arbitrary values, so reading them as text costs nothing in precision. The structural
cross-check that a real parser affords lives in `tests/test_dependencies.py`, where it can
skip on an old interpreter without the gate itself going quiet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"
CONFIG = ROOT / ".claude-workflow.json"

SECTION = re.compile(r"(?m)^\[([^\]]+)\]\s*$")
STATIC_DEPENDENCIES = re.compile(r"(?m)^dependencies\s*=\s*\[")
DYNAMIC_DEPENDENCIES = re.compile(r"(?m)^dynamic\s*=\s*\[(?P<items>[^\]]*)\]")
DELEGATION = re.compile(r"(?m)^dependencies\s*=\s*\{\s*file\s*=\s*\[(?P<files>[^\]]*)\]\s*\}")
# A requirement line: a name, optional extras and marker, and an optional version spec.
REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?\s*([<>=!~][^;]*)?(;.*)?$")


def sections(text: str) -> dict[str, str]:
    """Each `[table]` mapped to its body, so a key is checked in the table that owns it."""
    heads = list(SECTION.finditer(text))
    found: dict[str, str] = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        found[head.group(1)] = text[head.end() : end]
    return found


def quoted(values: str) -> list[str]:
    return re.findall(r"[\"']([^\"']+)[\"']", values)


def check_pyproject(text: str) -> list[str]:
    tables = sections(text)
    project = tables.get("project", "")
    failures: list[str] = []

    if STATIC_DEPENDENCIES.search(project):
        failures.append(
            "pyproject.toml: [project] declares a static dependencies list again, which is the "
            "duplication Issue #16 removed; a dependency added there and not to requirements.txt "
            "would ship unaudited, because pip-audit reads requirements.txt"
        )

    dynamic = DYNAMIC_DEPENDENCIES.search(project)
    if not dynamic or "dependencies" not in quoted(dynamic.group("items")):
        failures.append(
            'pyproject.toml: [project] must declare dynamic = ["dependencies"] so the wheel '
            "metadata is built from requirements.txt rather than from a second copy"
        )

    delegation = DELEGATION.search(tables.get("tool.setuptools.dynamic", ""))
    if not delegation:
        failures.append(
            "pyproject.toml: [tool.setuptools.dynamic] must map dependencies to a file, or the "
            "dynamic declaration has nothing to read"
        )
    elif quoted(delegation.group("files")) != ["requirements.txt"]:
        failures.append(
            "pyproject.toml: [tool.setuptools.dynamic] reads "
            f"{quoted(delegation.group('files'))}, but requirements.txt is the file pip-audit "
            "scans; reading anything else puts the shipped set back out of reach of the audit"
        )
    return failures


def check_requirements(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    entries = [line for line in lines if line and not line.startswith("#")]
    if not entries:
        return [
            "requirements.txt: no requirements found; it is the source the wheel metadata is "
            "built from, so an empty file would silently ship a package that depends on nothing"
        ]
    return [
        f"requirements.txt:{lines.index(entry) + 1}: {entry!r} is not a requirement setuptools "
        "can read as dependency metadata"
        for entry in entries
        if not REQUIREMENT.match(entry)
    ]


def check_audit_target(config_text: str) -> list[str]:
    """Whatever the audit scans has to be the file the package is built from."""
    try:
        commands = json.loads(config_text)["commands"]["security"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return [".claude-workflow.json: no security commands found to audit dependencies"]
    audits = [command for command in commands if "pip-audit" in command]
    if not audits:
        return [
            ".claude-workflow.json: the security stage runs no pip-audit, so runtime dependencies "
            "are never scanned"
        ]
    stray = [command for command in audits if "requirements.txt" not in command]
    return [
        f".claude-workflow.json: security command {command!r} does not audit requirements.txt, "
        "which is the file the package's dependencies are built from"
        for command in stray
    ]


def main() -> int:
    failures = check_pyproject(PYPROJECT.read_text(encoding="utf-8"))
    failures += check_requirements(REQUIREMENTS.read_text(encoding="utf-8"))
    failures += check_audit_target(CONFIG.read_text(encoding="utf-8"))
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        print(f"Dependency source check failed with {len(failures)} finding(s).")
        return 1
    print("Dependency source check passed: requirements.txt is the only declaration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
