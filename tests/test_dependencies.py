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
import re
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from check_dependencies import (  # noqa: E402
    check_artifacts,
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


def test_an_indented_static_list_does_not_slip_past():
    """TOML allows whitespace before a key, so one space restored the drift unnoticed."""
    text = DELEGATING.replace(
        'dynamic = ["dependencies"]',
        'dynamic = ["dependencies"]\n  dependencies = ["fastapi>=0.110"]',
    )
    assert any("static dependencies list again" in f for f in check_pyproject(text))


def test_the_sub_table_spelling_of_the_delegation_is_accepted():
    """Same TOML, written as its own section. Rejecting it refuses a correct config."""
    text = DELEGATING.replace(
        '[tool.setuptools.dynamic]\ndependencies = { file = ["requirements.txt"] }',
        '[tool.setuptools.dynamic.dependencies]\nfile = ["requirements.txt"]',
    )
    assert text != DELEGATING, "fixture replacement did not apply"
    assert check_pyproject(text) == []


def test_a_sub_table_delegation_to_the_wrong_file_is_still_rejected():
    text = DELEGATING.replace(
        '[tool.setuptools.dynamic]\ndependencies = { file = ["requirements.txt"] }',
        '[tool.setuptools.dynamic.dependencies]\nfile = ["deps.txt"]',
    )
    assert check_pyproject(text) != []


def test_a_table_heading_with_a_trailing_comment_is_still_a_heading():
    """Otherwise the body folds into the previous table and the gate stops looking."""
    text = DELEGATING.replace("[project]", "[project]  # the package")
    assert "project" in sections(text)
    assert check_pyproject(text) == []


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
        # setuptools strips an inline comment; verified against a real wheel build.
        pytest.param("fastapi>=0.110  # web framework", id="inline-comment"),
        pytest.param("pkg @ https://example.invalid/pkg.whl", id="pep508-direct-reference"),
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


def test_an_audit_no_stage_runs_is_rejected():
    """A correct command that nothing invokes audits nothing, and looks the same from here."""
    config = json.loads(CONFIG)
    for groups in config["stages"].values():
        while "security" in groups:
            groups.remove("security")
    failures = check_audit_target(json.dumps(config))
    assert any("no stage runs the security group" in failure for failure in failures)


def test_the_pr_gate_builds_a_source_distribution():
    """The sdist is where a dynamic dependency source goes missing, and only at release.

    setuptools does include a file referenced by [tool.setuptools.dynamic] without a
    MANIFEST.in, but nothing was proving that on every pull request.
    """
    config = json.loads(CONFIG)
    assert any("--sdist" in command for command in config["commands"]["build"])
    assert "build" in config["stages"]["pr"]


def test_the_check_runs_on_the_fast_stage():
    """Criterion: drift is caught before a pull request is opened, not at release."""
    config = json.loads(CONFIG)
    assert "dependency_sync" in config["stages"]["fast"]
    assert any("check_dependencies.py" in c for c in config["commands"]["dependency_sync"])


def _distributions(directory: Path, *, requirements: bool = True, requires: list[str] | None = None):
    """A minimal sdist and wheel pair, shaped like the real ones."""
    entries = ["fastapi>=0.110", "httpx>=0.27"] if requires is None else requires
    metadata = "Metadata-Version: 2.1\nName: demo\nVersion: 0.1.0\n" + "".join(
        f"Requires-Dist: {entry}\n" for entry in entries
    )
    metadata += 'Requires-Dist: pytest>=8.0; extra == "dev"\n'

    directory.mkdir(parents=True, exist_ok=True)
    payload = directory / "PKG-INFO"
    payload.write_text(metadata, encoding="utf-8")
    with tarfile.open(directory / "demo-0.1.0.tar.gz", "w:gz") as archive:
        archive.add(payload, arcname="demo-0.1.0/PKG-INFO")
        if requirements:
            source = directory / "requirements.txt"
            source.write_text("fastapi>=0.110\nhttpx>=0.27\n", encoding="utf-8")
            archive.add(source, arcname="demo-0.1.0/requirements.txt")
    with zipfile.ZipFile(directory / "demo-0.1.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("demo-0.1.0.dist-info/METADATA", metadata)
    return ["fastapi>=0.110", "httpx>=0.27"]


def test_matching_artifacts_pass(tmp_path):
    expected = _distributions(tmp_path / "dist")
    assert check_artifacts(tmp_path / "dist", expected) == []


def test_a_source_distribution_without_the_requirements_file_is_rejected(tmp_path):
    """The release-time failure: the dynamic source is missing and the sdist cannot build."""
    expected = _distributions(tmp_path / "dist", requirements=False)
    failures = check_artifacts(tmp_path / "dist", expected)
    assert any("not in the source distribution" in failure for failure in failures)


def test_an_artifact_that_declares_no_dependencies_is_rejected(tmp_path):
    """A wheel with no Requires-Dist installs cleanly and fails on the first import."""
    _distributions(tmp_path / "dist", requires=[])
    failures = check_artifacts(tmp_path / "dist", ["fastapi>=0.110", "httpx>=0.27"])
    assert any("declares nothing" in failure for failure in failures)


def test_artifacts_that_disagree_with_requirements_are_rejected(tmp_path):
    _distributions(tmp_path / "dist", requires=["fastapi>=0.110"])
    failures = check_artifacts(tmp_path / "dist", ["fastapi>=0.110", "httpx>=0.27"])
    assert any("do not match requirements.txt" in failure for failure in failures)


def test_a_missing_distribution_is_rejected(tmp_path):
    (tmp_path / "dist").mkdir()
    assert check_artifacts(tmp_path / "dist", ["fastapi>=0.110"]) != []


def test_extras_are_not_counted_as_runtime_dependencies(tmp_path):
    """`; extra == "dev"` entries are optional and must not be compared against the file."""
    expected = _distributions(tmp_path / "dist")
    assert check_artifacts(tmp_path / "dist", expected) == []


def test_the_build_step_verifies_its_own_artifacts():
    config = json.loads(CONFIG)
    build = config["commands"]["build"]
    assert any("--sdist" in command for command in build)
    assert any("--artifacts" in command for command in build)
    assert "build" in config["stages"]["pr"], "the verification must run on a pull request"


def test_the_wheel_metadata_is_built_from_the_requirements_file():
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
    # A superset, not an exact list: #23 adds loader libraries, and a test that has to be
    # edited for every new dependency is a test people delete. What must not happen is one
    # of these quietly disappearing, since the wheel now takes its metadata from here.
    names = {re.split(r"[<>=!~\[; @]", entry)[0] for entry in expected}
    assert names >= {
        "fastapi",
        "uvicorn",
        "httpx",
        "pydantic",
        "pydantic-settings",
        "chromadb",
        "pypdf",
        "python-docx",
        "python-multipart",
    }
