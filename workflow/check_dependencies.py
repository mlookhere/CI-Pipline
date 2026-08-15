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

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
REQUIREMENTS = ROOT / "requirements.txt"
CONFIG = ROOT / ".claude-workflow.json"
PACKAGE = ROOT / "knowledge_nexus"

# A table heading, tolerating indentation and a trailing comment. Anchoring on `$` alone
# would leave `[project]  # comment` unrecognised and silently fold its body into whichever
# table came before it, which is a gate that stops looking where it thinks it is looking.
SECTION = re.compile(r"(?m)^[ \t]*\[([^\]]+)\][ \t]*(?:#.*)?$")
# TOML permits whitespace before a key, so anchoring hard at the line start let a single
# leading space restore a static list with the gate none the wiser.
STATIC_DEPENDENCIES = re.compile(r"(?m)^[ \t]*dependencies[ \t]*=[ \t]*\[")
DYNAMIC_DEPENDENCIES = re.compile(r"(?m)^[ \t]*dynamic[ \t]*=[ \t]*\[(?P<items>[^\]]*)\]")
# The inline form, `dependencies = { file = ["requirements.txt"] }`. The equivalent
# sub-table form is read separately, from its own section.
DELEGATION = re.compile(r"(?m)^[ \t]*dependencies[ \t]*=[ \t]*\{[^}]*file[^[]*\[(?P<files>[^\]]*)\]")
DELEGATION_TABLE = re.compile(r"(?m)^[ \t]*file[ \t]*=[ \t]*\[(?P<files>[^\]]*)\]")
# A direct `HttpClient(` / `AsyncHttpClient(` call. This is a backstop, not a proof: an
# alias, a `getattr`, or a name bound and then called all evade it. The property that
# actually holds is enforced at run time, by `_require_local_api` refusing any client that
# did not resolve to the embedded implementation.
QUOTES = ('"""', "'''", '"', "'")
CHROMA_SERVER_CLIENT = re.compile(r"\b(?:Async)?HttpClient\s*\(")
# A requirement as setuptools will read it: a name, optional extras, then either a version
# specifier or a PEP 508 direct reference, then an optional marker. Inline comments are
# stripped before matching -- setuptools strips them too, verified against a real build.
REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[^\]]+\])?"
    r"[ \t]*(?:[<>=!~][^;]*|@[ \t]+\S+)?"
    r"(?:;.*)?$"
)


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

    # Both spellings are the same TOML: an inline table under [tool.setuptools.dynamic], or
    # its own [tool.setuptools.dynamic.dependencies] section. Rejecting the second would be
    # a gate refusing a correct configuration.
    inline = DELEGATION.search(tables.get("tool.setuptools.dynamic", ""))
    table = DELEGATION_TABLE.search(tables.get("tool.setuptools.dynamic.dependencies", ""))
    delegation = inline or table
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
    """Every line must be something setuptools can turn into dependency metadata.

    This file is read by pip *and* by the build backend, and the two do not accept the same
    grammar: `-r other.txt` and `--index-url` are pip input, not metadata. Catching that
    here is the point, because the wheel is where it would otherwise surface.

    An inline `#` comment is fine -- setuptools strips it, confirmed against a real build --
    so it is stripped here too rather than reported.
    """
    entries = [
        (number, stripped)
        for number, line in enumerate(text.splitlines(), start=1)
        if (stripped := line.split("#")[0].strip())
    ]
    if not entries:
        return [
            "requirements.txt: no requirements found; it is the source the wheel metadata is "
            "built from, so an empty file would silently ship a package that depends on nothing"
        ]
    return [
        f"requirements.txt:{number}: {entry!r} is not a requirement setuptools can read as "
        "dependency metadata"
        for number, entry in entries
        if not REQUIREMENT.match(entry)
    ]


def check_audit_target(config_text: str) -> list[str]:
    """Whatever the audit scans has to be the file the package is built from.

    And it has to actually run. A correct `security` command that no stage invokes audits
    nothing, which looks identical to being audited from in here.
    """
    try:
        config = json.loads(config_text)
        commands = config["commands"]["security"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return [".claude-workflow.json: no security commands found to audit dependencies"]
    stages = [name for name, groups in config.get("stages", {}).items() if "security" in groups]
    if not stages:
        return [
            ".claude-workflow.json: no stage runs the security group, so the dependency audit "
            "never executes however it is configured"
        ]
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


def declared(text: str) -> list[str]:
    """The requirement lines, as setuptools will read them."""
    return [stripped for line in text.splitlines() if (stripped := line.split("#")[0].strip())]


def metadata_requirements(blob: str) -> list[str]:
    """Runtime `Requires-Dist` entries, ignoring anything gated behind an extra."""
    return [
        value.strip()
        for line in blob.splitlines()
        if line.startswith("Requires-Dist:") and "extra ==" not in (value := line.split(":", 1)[1])
    ]


def check_artifacts(directory: Path, expected: list[str]) -> list[str]:
    """The declaration being right is not the same as the artifact being right.

    Reading dependencies from a file means the file has to reach the source distribution,
    and setuptools includes it only because it notices the reference -- there is no
    `MANIFEST.in` saying so. A pruning manifest, or a change of build backend, would
    produce an sdist that cannot be built, and that would surface at release rather than
    here. The other half is quieter still: a `requirements.txt` that is present but
    unreadable at build time yields a wheel with no dependencies at all, which installs
    cleanly and fails on the first import.
    """
    import tarfile
    import zipfile

    failures: list[str] = []
    sdists = sorted(directory.glob("*.tar.gz"))
    wheels = sorted(directory.glob("*.whl"))
    if not sdists or not wheels:
        return [
            f"{directory}: expected a source distribution and a wheel to inspect; "
            f"found {len(sdists)} sdist(s) and {len(wheels)} wheel(s)"
        ]

    with tarfile.open(sdists[-1]) as archive:
        names = archive.getnames()
        if not any(name.endswith("/requirements.txt") for name in names):
            failures.append(
                f"{sdists[-1].name}: requirements.txt is not in the source distribution, so the "
                "dynamic dependency source is missing and the sdist cannot be built"
            )
        info = next((name for name in names if name.endswith("PKG-INFO")), None)
        if info:
            handle = archive.extractfile(info)
            content = handle.read().decode("utf-8") if handle else ""
            failures += compare(sdists[-1].name, metadata_requirements(content), expected)

    with zipfile.ZipFile(wheels[-1]) as wheel:
        name = next((item for item in wheel.namelist() if item.endswith("METADATA")), None)
        if name:
            content = wheel.read(name).decode("utf-8")
            failures += compare(wheels[-1].name, metadata_requirements(content), expected)
    return failures


def normalise(entry: str) -> tuple[str, tuple[str, ...]]:
    """A requirement reduced to what it means, not how it was written.

    setuptools reorders a multi-clause specifier -- `chromadb>=0.5,<1.0` comes back as
    `chromadb<1.0,>=0.5` -- so comparing the raw strings reports a mismatch between a
    requirement and itself. That went unnoticed while every requirement had one clause.
    """
    text = "".join(entry.split())
    marker = ""
    if ";" in text:
        text, marker = text.split(";", 1)
    for index, char in enumerate(text):
        if char in "<>=!~@":
            name, clauses = text[:index], text[index:]
            break
    else:
        name, clauses = text, ""
    # PEP 503 canonicalisation: a dot and an underscore are the same separator, so
    # `zope.interface` and `zope-interface` are one package and must compare equal.
    canonical = re.sub(r"[-_.]+", "-", name).lower()
    parts = tuple(sorted(part for part in clauses.split(",") if part))
    # setuptools emits double-quoted marker strings where the source file may use single
    # quotes; the marker means the same thing either way.
    return canonical, parts + ((";" + marker.replace("'", '"'),) if marker else ())


def compare(artifact: str, found: list[str], expected: list[str]) -> list[str]:
    if sorted(map(normalise, found)) == sorted(map(normalise, expected)):
        return []
    return [
        f"{artifact}: runtime dependencies do not match requirements.txt; "
        f"the artifact declares {found or 'nothing'} and requirements.txt declares {expected}"
    ]


def code_only(line: str) -> str:
    """The line with comments and string bodies removed.

    Writing `HttpClient(` inside a docstring in order to *forbid* it should not break the
    fast gate, and neither should a trailing comment that mentions it. Only code counts.
    """
    kept: list[str] = []
    quote = ""
    index = 0
    while index < len(line):
        if quote:
            if line.startswith(quote, index):
                index += len(quote)
                quote = ""
            else:
                index += 1
            continue
        opened = next((mark for mark in QUOTES if line.startswith(mark, index)), "")
        if opened:
            quote = opened
            index += len(opened)
            continue
        if line[index] == "#":
            break
        kept.append(line[index])
        index += 1
    return "".join(kept)


def check_no_chroma_server_client() -> list[str]:
    """The advisory this repository is pinned around is a *server* vulnerability.

    PYSEC-2026-311 is a pre-authentication code injection reachable through Chroma's HTTP
    surface. `chromadb>=0.5,<1.0` keeps this install off every affected version, but that
    pin is only half the argument: the other half is that the embedded client never opens
    that surface at all. `HttpClient` or `AsyncHttpClient` would, and would do it quietly,
    because nothing else in the build would change.

    So the pin and this check hold the position together. Adopting a Chroma server means
    reassessing the advisory first, deliberately, rather than discovering it later.
    """
    failures: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if CHROMA_SERVER_CLIENT.search(code_only(line)):
                failures.append(
                    f"{path.relative_to(ROOT).as_posix()}:{number}: uses a Chroma HTTP client, "
                    "which exposes the surface PYSEC-2026-311 targets; the pin to chromadb<1.0 "
                    "assumes embedded use, so reassess that advisory before adopting a server"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep runtime dependencies declared once.")
    parser.add_argument(
        "--artifacts",
        type=Path,
        help="also verify built distributions in this directory against requirements.txt",
    )
    args = parser.parse_args()

    requirements = REQUIREMENTS.read_text(encoding="utf-8")
    failures = check_pyproject(PYPROJECT.read_text(encoding="utf-8"))
    failures += check_requirements(requirements)
    failures += check_audit_target(CONFIG.read_text(encoding="utf-8"))
    failures += check_no_chroma_server_client()
    if args.artifacts:
        failures += check_artifacts(args.artifacts, declared(requirements))
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        print(f"Dependency source check failed with {len(failures)} finding(s).")
        return 1
    scope = "declaration and built artifacts" if args.artifacts else "declaration"
    print(f"Dependency source check passed ({scope}): requirements.txt is the only declaration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
