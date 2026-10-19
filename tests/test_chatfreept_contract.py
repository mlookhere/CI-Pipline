"""Contract regressions: missing work, stale metadata and absent CI jobs fail closed."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

import chatfreept_contract as contract  # noqa: E402

spec = importlib.util.spec_from_file_location("ci_run", ROOT / "ci" / "run.py")
assert spec is not None and spec.loader is not None
ci_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_run)


@pytest.fixture
def inputs():
    project = json.loads((ROOT / ".chatfreept" / "project.json").read_text(encoding="utf-8"))
    legacy = json.loads((ROOT / ".chatfreept" / "github.json").read_text(encoding="utf-8"))
    workflows = {
        path: (ROOT / path).read_text(encoding="utf-8")
        for paths in contract.WORKFLOWS.values()
        for path in paths
    }
    return project, legacy, workflows


def test_committed_contract_matches_executable_ci(inputs):
    assert contract.check_project(*inputs) == []
    assert contract.validate_repository(ROOT) == []
    for stage in ("fast", "pr", "release", "nightly", "audit", "bootstrap-ci"):
        assert ci_run.stage_commands(stage, inputs[1]), stage


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("version", 2, "schema version"),
        ("repository", "another/project", "repository identity"),
        ("agent", {"provider": "claude"}, "agent."),
        ("branches", {"integration": "main", "production": "master"}, "integration branch"),
        ("required_checks", {"integration": [], "production": []}, "required checks"),
        ("slices", [], "slices must be numbered"),
        ("commands", {}, "bootstrap"),
    ],
)
def test_contract_refuses_invalid_metadata(inputs, field, value, expected):
    project, legacy, workflows = copy.deepcopy(inputs)
    project[field] = value
    assert any(expected in failure for failure in contract.check_project(project, legacy, workflows))


def test_missing_required_job_is_rejected_even_if_legacy_config_agrees(inputs):
    project, legacy, workflows = copy.deepcopy(inputs)
    workflow = ".github/workflows/ci-pr.yml"
    workflows[workflow] = workflows[workflow].replace(
        "    name: Fast deterministic gates", "    name: Renamed fast"
    )
    assert any(
        "Fast deterministic gates" in message
        for message in contract.check_project(project, legacy, workflows)
    )


def test_stale_production_metadata_is_rejected(inputs):
    project, legacy, workflows = copy.deepcopy(inputs)
    legacy["branches"]["production"] = "main"
    assert any(
        "production branch" in message for message in contract.check_project(project, legacy, workflows)
    )


def test_unreadable_manifest_fails_closed(tmp_path):
    (tmp_path / ".chatfreept").mkdir()
    (tmp_path / ".chatfreept" / "project.json").write_text("{oops", encoding="utf-8")
    assert contract.validate_repository(tmp_path)


@pytest.mark.parametrize(
    "groups",
    [
        [],
        ["unknown"],
        ["empty"],
        ["blank"],
        ["unit", "empty"],
    ],
)
def test_runner_rejects_empty_or_undefined_groups(groups):
    config = {"stages": {"pr": groups}, "commands": {"unit": ["echo fine"], "empty": [], "blank": [" "]}}
    with pytest.raises(SystemExit, match="no|empty|missing"):
        ci_run.stage_commands("pr", config)


def test_runner_accepts_quality_sentinel():
    assert ci_run.stage_commands("fast", {"stages": {"fast": ["quality"]}, "commands": {}}) == [
        ("quality", "__QUALITY__")
    ]


def test_runner_cannot_report_success_without_executing_a_command(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_run, "load_config", lambda: {"stages": {"pr": []}, "commands": {}})
    monkeypatch.setattr(ci_run, "ARTIFACT_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["ci/run.py", "pr"])
    with pytest.raises(SystemExit, match="no command groups"):
        ci_run.main()
    assert not list(tmp_path.glob("*.json"))


def test_ci_home_is_chatfreept_owned(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATFREEPT_CI_HOME", str(tmp_path / "runtime"))
    assert ci_run.ci_home() == tmp_path / "runtime"
    monkeypatch.delenv("CHATFREEPT_CI_HOME")
    assert ci_run.ci_home() == Path.home() / ".local" / "share" / "chatfreept-ci" / "v1"


def test_bootstrap_and_runner_use_same_home_configuration():
    script = (ROOT / "scripts" / "bootstrap").read_text(encoding="utf-8")
    assert "CHATFREEPT_CI_HOME" in script
    assert "chatfreept-ci/v1" in script
    assert "CLAUDE_CI_HOME" not in script


def test_policy_carries_required_repository_configuration():
    from chatfreept_stages import read_config

    policy = json.loads((ROOT / ".chatfreept" / "github.json").read_text(encoding="utf-8"))
    stages = read_config(ROOT)
    assert policy["github"]["expected_repository"] == "CI-Pipline"
    assert policy["commands"] == stages["commands"]
    assert policy["stages"] == stages["stages"]
    assert "project" in policy


def test_shared_shell_resolves_the_same_chatfreept_ci_runtime():
    script = (ROOT / "scripts" / "lib" / "python.sh").read_text(encoding="utf-8")
    assert "CHATFREEPT_CI_HOME" in script
    assert "CHATFREEPT_CI_PYTHON" in script
    assert "chatfreept-ci/v1" in script
    assert "CLAUDE_CI_HOME" not in script
