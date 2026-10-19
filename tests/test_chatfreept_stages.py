"""Runtime-neutral stage parity and negative configuration cases."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_stages as neutral  # noqa: E402

spec = importlib.util.spec_from_file_location("ci_run", ROOT / "ci" / "run.py")
assert spec is not None and spec.loader is not None
ci_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_run)


@pytest.fixture
def inputs():
    modern = json.loads((ROOT / neutral.CONFIG_PATH).read_text(encoding="utf-8"))
    legacy = json.loads((ROOT / neutral.LEGACY_PATH).read_text(encoding="utf-8"))
    return modern, legacy


def test_committed_stage_manifest_has_parity_and_real_commands(inputs):
    modern, legacy = inputs
    assert neutral.validate_config(modern, legacy) == []
    assert neutral.read_config(ROOT) == modern
    for stage in neutral.STAGES:
        assert ci_run.stage_commands(stage, modern) == ci_run.stage_commands(stage, legacy)


@pytest.mark.parametrize(
    ("field", "bad", "message"),
    [
        ("version", 2, "schema version"),
        ("commands", {}, "commands must be"),
        ("stages", {}, "stage 'fast'"),
        ("quality", {}, "quality policy"),
    ],
)
def test_missing_core_configuration_fails(inputs, field, bad, message):
    modern, legacy = copy.deepcopy(inputs)
    modern[field] = bad
    assert any(message in error for error in neutral.validate_config(modern, legacy))


def test_unknown_top_level_key_rejected(inputs):
    modern, legacy = copy.deepcopy(inputs)
    modern["claude_hook"] = "ignored"
    assert any("unsupported keys" in error for error in neutral.validate_config(modern, legacy))


def test_undefined_stage_rejected(inputs):
    modern, legacy = copy.deepcopy(inputs)
    modern["stages"]["skipper"] = ["unit"]
    assert any("unsupported stage names" in error for error in neutral.validate_config(modern, legacy))


@pytest.mark.parametrize("command", ["", " ", "true", ":", "# silently skip", "TODO", "CHANGE_ME"])
def test_empty_and_placeholder_commands_are_rejected(inputs, command):
    modern, legacy = copy.deepcopy(inputs)
    modern["commands"]["unit"] = [command]
    assert any("placeholder" in error for error in neutral.validate_config(modern, legacy))


def test_group_with_no_caller_cannot_be_used_to_hide_missing_stage(inputs):
    modern, legacy = copy.deepcopy(inputs)
    modern["stages"]["pr"] = ["unit", "invented"]
    assert any("unknown group" in error for error in neutral.validate_config(modern, legacy))


def test_legacy_divergence_fails_before_running_commands(inputs):
    modern, legacy = copy.deepcopy(inputs)
    legacy["commands"]["unit"] = ["exit 0"]
    assert any("differs from legacy CI" in error for error in neutral.validate_config(modern, legacy))


def test_bad_json_causes_error_not_success(tmp_path):
    target = tmp_path / ".chatfreept"
    target.mkdir()
    (target / "ci.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        neutral.read_config(tmp_path)
