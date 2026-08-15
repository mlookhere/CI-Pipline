"""Runtime dependencies must be declared in exactly one place.

`requirements.txt` and `[project].dependencies` held the same nine lines, nothing compared
them, and only `requirements.txt` was ever audited -- so a dependency added to
`pyproject.toml` alone would ship unscanned (Issue #16). `pyproject.toml` now reads
`requirements.txt`, which makes the two incapable of disagreeing.

The way that breaks is not a mismatch appearing. It is someone re-adding a static list, at
which point the drift is back and nothing says so, so that is what these tests aim at.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from check_dependencies import (  # noqa: E402
    check_audit_target,
    check_pyproject,
    check_requirements,
    sections,
)

PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
CONFIG = (ROOT / ".claude-workflow.json").read_text(encoding="utf-8")

DELEGATING = """[project]
name = "demo"
requires-python = ">=3.10"
dynamic = ["dependencies"]

[tool.setuptools.dynamic]
dependencies = { file = ["requirements.txt"] }
"""


def test_this_repository_declares_dependencies_once():
    assert check_pyproject(PYPROJECT) == []
    assert check_requirements(REQUIREMENTS) == []
    assert check_audit_target(CONFIG) == []


def test_the_delegating_shape_passes():
    assert check_pyproject(DELEGATING) == []


def test_a_reintroduced_static_list_fails_the_gate():
    """The deliberate mismatch: the exact edit that would bring the drift back."""
    text = DELEGATING.replace(
        'dynamic = ["dependencies"]',
        'dependencies = [\n    "fastapi>=0.110",\n]',
    )
    failures = check_pyproject(text)
    assert any("static dependencies list again" in failure for failure in failures)


def test_a_static_list_alongside_the_dynamic_declaration_still_fails():
    """Setuptools rejects this combination too, but the gate must not rely on that."""
    text = DELEGATING.replace(
        'dynamic = ["dependencies"]',
        'dynamic = ["dependencies"]\ndependencies = [\n    "fastapi>=0.110",\n]',
    )
    assert any("static dependencies list again" in f for f in check_pyproject(text))


def test_dropping_the_dynamic_declaration_fails():
    text = DELEGATING.replace('dynamic = ["dependencies"]\n', "")
    assert any('dynamic = ["dependencies"]' in failure for failure in check_pyproject(text))


def test_a_dynamic_declaration_for_something_else_does_not_count():
    text = DELEGATING.replace('dynamic = ["dependencies"]', 'dynamic = ["version"]')
    assert any('dynamic = ["dependencies"]' in failure for failure in check_pyproject(text))


def test_delegating_to_a_different_file_fails():
    """Reading anything but requirements.txt puts the shipped set outside the audit."""
    text = DELEGATING.replace('file = ["requirements.txt"]', 'file = ["deps.txt"]')
    failures = check_pyproject(text)
    assert any(
        "pip-audit scans" in failure or "requirements.txt is the file" in failure for failure in failures
    )


def test_a_missing_delegation_fails():
    text = DELEGATING.replace(
        '[tool.setuptools.dynamic]\ndependencies = { file = ["requirements.txt"] }\n', ""
    )
    assert any("nothing to read" in failure for failure in check_pyproject(text))


def test_a_dependencies_key_in_another_table_is_not_read_as_the_projects():
    """`[project]` is the only table whose dependencies key means this."""
    text = DELEGATING + '\n[tool.something]\ndependencies = ["not-the-project"]\n'
    assert check_pyproject(text) == []


def test_sections_splits_on_table_headings():
    assert sorted(sections(DELEGATING)) == ["project", "tool.setuptools.dynamic"]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("fastapi>=0.110", id="pinned"),
        pytest.param("fastapi", id="unpinned"),
        pytest.param("uvicorn[standard]>=0.29", id="extras"),
        pytest.param("tomli>=2.0; python_version < '3.11'", id="marker"),
    ],
)
def test_a_requirement_line_the_gate_must_accept(line):
    assert check_requirements(f"{line}\n") == []


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("-r other.txt\n", id="include-directive"),
        pytest.param("https://example.invalid/pkg.whl\n", id="direct-url"),
        pytest.param("--index-url https://example.invalid\n", id="pip-option"),
    ],
)
def test_a_requirement_line_setuptools_cannot_read_as_metadata_is_rejected(text):
    """setuptools reads this file as dependency metadata, not as pip input."""
    assert check_requirements(text) != []


def test_an_empty_requirements_file_is_rejected():
    assert any("depends on nothing" in failure for failure in check_requirements("# only a comment\n"))


def test_an_audit_that_scans_a_different_file_is_rejected():
    text = CONFIG.replace("pip-audit -r requirements.txt", "pip-audit -r deps.txt")
    assert text != CONFIG, "the audit command moved; this test is pinned to stale text"
    assert any("does not audit requirements.txt" in failure for failure in check_audit_target(text))


def test_removing_the_audit_entirely_is_rejected():
    config = json.loads(CONFIG)
    config["commands"]["security"] = ["echo nothing to see"]
    assert any("runs no pip-audit" in failure for failure in check_audit_target(json.dumps(config)))


def test_the_check_runs_on_the_fast_stage():
    """Criterion: drift is caught before a pull request is opened, not at release."""
    config = json.loads(CONFIG)
    assert "dependency_sync" in config["stages"]["fast"]
    assert any("check_dependencies.py" in c for c in config["commands"]["dependency_sync"])


def test_the_declared_dependencies_are_exactly_the_requirements_file():
    """The structural cross-check, with a real parser rather than by line.

    Skips below 3.11 rather than weakening the gate: `requires-python` is >=3.10 and the
    fast gate runs under whatever `scripts/bootstrap` resolved, so `check_dependencies`
    itself stays parser-free. Hosted CI pins 3.12, so this does run where it counts.
    """
    tomllib = pytest.importorskip("tomllib", reason="tomllib arrived in Python 3.11")
    parsed = tomllib.loads(PYPROJECT)
    assert "dependencies" in parsed["project"]["dynamic"]
    assert "dependencies" not in parsed["project"], "a static list is back"
    assert parsed["tool"]["setuptools"]["dynamic"]["dependencies"]["file"] == ["requirements.txt"]
    expected = [
        line.strip()
        for line in REQUIREMENTS.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert expected, "requirements.txt carries no requirements"
    # The nine that were duplicated. Named here so removing one is a deliberate edit.
    assert [name.split(">")[0].split("=")[0] for name in expected] == [
        "fastapi",
        "uvicorn",
        "httpx",
        "pydantic",
        "pydantic-settings",
        "chromadb",
        "pypdf",
        "python-docx",
        "python-multipart",
    ]
