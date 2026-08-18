"""`workflow/check_dependencies.py`, exercised without a package to point it at.

The module keeps a consumer's runtime dependencies declared in exactly one place: a
`requirements.txt` and a `[project].dependencies` list holding the same lines will drift,
and only one of them is ever audited, so a dependency added to `pyproject.toml` alone ships
unscanned. The fix it guards is delegation rather than comparison -- `pyproject.toml`
declares its dependencies `dynamic` and reads `requirements.txt`, so the two cannot
disagree -- and the way that breaks is not a mismatch appearing but someone re-adding a
static list, at which point the drift is back and nothing says so.

This repository has no package, no `requirements.txt` and no stage that runs the module. It
ships it for consumers that do, which is exactly why the behaviour is pinned here: the tests
that read a real `pyproject.toml` went with the extraction, and what is left constructs
every input it checks, so the module stays covered by a repository that cannot use it.
"""

from __future__ import annotations

import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

import check_dependencies  # noqa: E402
from check_dependencies import (  # noqa: E402
    check_artifacts,
    check_audit_target,
    check_forbidden_call_sites,
    check_packaged_assets,
    check_pyproject,
    check_requirements,
    compare,
    main,
    normalise,
    packaged_assets,
    sections,
)

DELEGATING = """[project]
name = "demo"
requires-python = ">=3.10"
dynamic = ["dependencies"]

[tool.setuptools.dynamic]
dependencies = { file = ["requirements.txt"] }
"""

# A consumer's `project` block, supplied by the tests that need one. The rule below is the
# one the extracted project declares, kept verbatim as the fixture because its regex is the
# thing being pinned: it shipped once with a literal backspace where the `\\b` belonged,
# matched nothing, and reported safety it had never checked.
HTTP_CLIENT_RULE = {
    "pattern": r"\b(?:Async)?HttpClient\s*\(",
    "message": "opens the HTTP surface the pinned advisory assumes is unused",
}
CONSUMER_CONFIG = {
    "commands": {"security": ["pip-audit -r requirements.txt --strict"]},
    "stages": {"audit": ["security"]},
}


def consumer(tmp_path: Path, monkeypatch, **project_fields) -> Path:
    """A stand-in for a repository that has what this one does not, returning its package.

    `check_dependencies` reads a package directory, a `pyproject.toml`, a `requirements.txt`
    and a `project` block from the repository it runs in. This repository has none of them,
    deliberately. Constructing them per test is what keeps these assertions about the
    module's behaviour rather than about a repository that would have to grow a package to
    keep them passing.
    """
    import check_dependencies

    root = tmp_path / "consumer"
    package = root / "demo"
    package.mkdir(parents=True)
    (root / "requirements.txt").write_text("fastapi>=0.110\nhttpx>=0.27\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(DELEGATING, encoding="utf-8")
    (root / ".claude-workflow.json").write_text(json.dumps(CONSUMER_CONFIG), encoding="utf-8")

    monkeypatch.setattr(check_dependencies, "ROOT", root)
    monkeypatch.setattr(check_dependencies, "requirements_path", lambda: root / "requirements.txt")
    monkeypatch.setattr(check_dependencies, "PYPROJECT", root / "pyproject.toml")
    monkeypatch.setattr(check_dependencies, "CONFIG", root / ".claude-workflow.json")
    monkeypatch.setattr(check_dependencies, "project", lambda: project_fields)
    monkeypatch.setattr(check_dependencies, "package_dir", lambda: package)
    return package


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


def _distributions(
    directory: Path,
    *,
    requirements: bool = True,
    requires: list[str] | None = None,
    assets: bool = True,
):
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
        if assets:
            for asset in packaged_assets():
                archive.add(payload, arcname=f"demo-0.1.0/{asset}")
        if requirements:
            source = directory / "requirements.txt"
            source.write_text("fastapi>=0.110\nhttpx>=0.27\n", encoding="utf-8")
            archive.add(source, arcname="demo-0.1.0/requirements.txt")
    with zipfile.ZipFile(directory / "demo-0.1.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr("demo-0.1.0.dist-info/METADATA", metadata)
        if assets:
            for asset in packaged_assets():
                wheel.writestr(asset, "<!doctype html>")
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


# ------------------------------------------------- the forbidden call-site guard


def test_a_repository_that_declares_no_forbidden_call_sites_reports_none():
    """The default an adopter starts from, and the state this repository stays in."""
    assert check_forbidden_call_sites() == []


def test_a_declared_call_site_guard_actually_fires(tmp_path, monkeypatch):
    """A guard that cannot fire is worse than none: it reports safety it never checked.

    This one shipped with a literal backspace where its `\b` should have been, matched
    nothing, and passed.
    """
    package = consumer(tmp_path, monkeypatch, forbidden_call_sites=[HTTP_CLIENT_RULE])
    (package / "store.py").write_text(
        "import vendor\nclient = vendor.HttpClient(host='service.internal')\n",
        encoding="utf-8",
    )
    failures = check_forbidden_call_sites()
    assert len(failures) == 1
    assert HTTP_CLIENT_RULE["message"] in failures[0]


def test_the_guard_ignores_a_call_the_rule_does_not_name(tmp_path, monkeypatch):
    package = consumer(tmp_path, monkeypatch, forbidden_call_sites=[HTTP_CLIENT_RULE])
    (package / "store.py").write_text(
        "import vendor\nclient = vendor.PersistentClient(path='data')\n", encoding="utf-8"
    )
    assert check_forbidden_call_sites() == []


def test_a_repository_that_declares_no_rules_is_not_scanned(tmp_path, monkeypatch):
    """The default an adopter starts from: the mechanism is generic, the rules are theirs."""
    package = consumer(tmp_path, monkeypatch, forbidden_call_sites=[])
    (package / "store.py").write_text(
        "import vendor\nclient = vendor.HttpClient(host='service.internal')\n", encoding="utf-8"
    )
    assert check_forbidden_call_sites() == []


def test_a_reordered_specifier_is_the_same_requirement():
    """setuptools rewrites `chromadb>=0.5,<1.0` as `chromadb<1.0,>=0.5`.

    Comparing raw strings reported a requirement as disagreeing with itself, and the
    defect was invisible while every requirement had a single clause.
    """
    assert compare("demo.whl", ["chromadb<1.0,>=0.5"], ["chromadb>=0.5,<1.0"]) == []


def test_a_genuinely_different_bound_is_still_caught():
    assert compare("demo.whl", ["chromadb<2.0,>=0.5"], ["chromadb>=0.5,<1.0"]) != []


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param("uvicorn[standard]>=0.29", "uvicorn[standard]>=0.29", id="extras"),
        pytest.param("Python_Docx>=1.1", "python-docx>=1.1", id="name-normalisation"),
        pytest.param(
            "tomli>=2.0; python_version<'3.11'", "tomli>=2.0;python_version<'3.11'", id="marker-spacing"
        ),
    ],
)
def test_equivalent_spellings_compare_equal(left, right):
    assert normalise(left) == normalise(right)


def test_the_guard_also_catches_the_async_spelling(tmp_path, monkeypatch):
    package = consumer(tmp_path, monkeypatch, forbidden_call_sites=[HTTP_CLIENT_RULE])
    (package / "store.py").write_text(
        "import vendor\nc = vendor.AsyncHttpClient(host='service.internal')\n",
        encoding="utf-8",
    )
    assert len(check_forbidden_call_sites()) == 1


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('"""Never construct HttpClient(host) here."""\n', id="docstring"),
        pytest.param("x = 1  # not an HttpClient(...) call\n", id="trailing-comment"),
        pytest.param("name = 'HttpClient('\n", id="string-literal"),
    ],
)
def test_the_guard_does_not_fire_on_prose(tmp_path, monkeypatch, source):
    """Documenting the prohibition must not break the gate that enforces it."""
    package = consumer(tmp_path, monkeypatch, forbidden_call_sites=[HTTP_CLIENT_RULE])
    (package / "store.py").write_text(source, encoding="utf-8")
    assert check_forbidden_call_sites() == []


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param("zope.interface>=5", "zope-interface>=5", id="pep503-dot"),
        pytest.param("Zope_Interface>=5", "zope-interface>=5", id="pep503-mixed"),
        pytest.param(
            "tomli>=2.0; python_version<'3.11'",
            'tomli>=2.0; python_version<"3.11"',
            id="marker-quote-style",
        ),
    ],
)
def test_equivalent_names_and_markers_compare_equal(left, right):
    """setuptools rewrites both; comparing the spellings reported a false mismatch."""
    assert normalise(left) == normalise(right)


ASSET = "demo/web/index.html"


def test_distributions_that_ship_the_declared_asset_pass(tmp_path, monkeypatch):
    consumer(tmp_path, monkeypatch, packaged_assets=[ASSET])
    _distributions(tmp_path / "dist")
    assert check_packaged_assets(tmp_path / "dist") == []


def test_a_wheel_without_the_declared_asset_is_rejected(tmp_path, monkeypatch):
    """The shipped failure this guards: every API route answers and GET / returns a 500."""
    consumer(tmp_path, monkeypatch, packaged_assets=[ASSET])
    _distributions(tmp_path / "dist", assets=False)
    failures = check_packaged_assets(tmp_path / "dist")
    assert any(ASSET in failure and "500" in failure for failure in failures)


def test_a_source_distribution_without_the_declared_asset_is_rejected(tmp_path, monkeypatch):
    """A wheel is only ever as complete as the sdist a rebuild would start from."""
    consumer(tmp_path, monkeypatch, packaged_assets=[ASSET])
    _distributions(tmp_path / "dist", assets=False)
    failures = check_packaged_assets(tmp_path / "dist")
    assert any("would ship without it" in failure for failure in failures)


def test_a_repository_declaring_no_assets_has_nothing_to_check(tmp_path, monkeypatch):
    """Empty is the adopter's default, and must not read as every asset being missing."""
    consumer(tmp_path, monkeypatch, packaged_assets=[])
    _distributions(tmp_path / "dist", assets=False)
    assert check_packaged_assets(tmp_path / "dist") == []


def test_the_asset_check_reports_a_directory_with_nothing_to_read(tmp_path):
    """Nothing to inspect is a failure, not a pass: a vacuous check reads as a green gate."""
    (tmp_path / "dist").mkdir()
    assert check_packaged_assets(tmp_path / "dist") != []


def test_the_artifacts_flag_runs_the_asset_check(tmp_path, monkeypatch, capsys):
    """Wiring, not logic: a check nothing invokes verifies nothing, and looks identical."""
    consumer(tmp_path, monkeypatch, packaged_assets=[ASSET])
    _distributions(tmp_path / "dist", assets=False)
    monkeypatch.setattr(sys, "argv", ["check_dependencies.py", "--artifacts", str(tmp_path / "dist")])
    assert main() == 1
    assert f"does not contain {ASSET}" in capsys.readouterr().out


# ------------------------------------------------- the manifest name is the consumer's


def test_the_audit_target_defaults_to_requirements_txt(tmp_path, monkeypatch):
    """An adopter that declares nothing keeps the behaviour this module was written with."""
    consumer(tmp_path, monkeypatch)

    assert check_dependencies.manifest() == "requirements.txt"
    assert check_audit_target(json.dumps(CONSUMER_CONFIG)) == []


def test_an_audit_of_a_differently_named_manifest_is_accepted(tmp_path, monkeypatch):
    """The defect: a correct configuration was rejected for not naming a literal.

    This repository audits `ci/requirements-ci.txt`, which does not contain the substring
    `requirements.txt`, so every consumer whose manifest sits anywhere else failed a check
    that was right about the requirement and wrong about the filename (Issue #9).
    """
    consumer(tmp_path, monkeypatch, dependency_manifest="ci/requirements-ci.txt")
    config = {
        "commands": {"security": ["pip-audit -r ci/requirements-ci.txt --strict"]},
        "stages": {"audit": ["security"]},
    }

    assert check_audit_target(json.dumps(config)) == []


def test_an_audit_that_scans_a_different_file_is_still_rejected(tmp_path, monkeypatch):
    """Configurable is not unenforced: it must still be the declared manifest."""
    consumer(tmp_path, monkeypatch, dependency_manifest="ci/requirements-ci.txt")
    config = {
        "commands": {"security": ["pip-audit -r somewhere-else.txt --strict"]},
        "stages": {"audit": ["security"]},
    }

    assert check_audit_target(json.dumps(config)) != []


def test_the_delegation_must_read_the_declared_manifest(tmp_path, monkeypatch):
    consumer(tmp_path, monkeypatch, dependency_manifest="deps.txt")

    assert any("deps.txt is the file" in failure for failure in check_pyproject(DELEGATING))
