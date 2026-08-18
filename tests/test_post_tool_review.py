"""`failure_count`, which reads state a crashed session may have left half-written."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

# E402: the hooks directory is not an importable package, so sys.path has to be extended
# first. Suppressed for that reason alone, matching tests/test_pre_tool_policy.py.
import post_tool_review  # noqa: E402


def state_file(tmp_path: Path, contents: str | None = None) -> Path:
    path = post_tool_review.state_dir(tmp_path) / "failures.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    return path


def test_repeated_failures_are_counted(tmp_path):
    assert post_tool_review.failure_count(tmp_path, "pytest -q", True) == 1
    assert post_tool_review.failure_count(tmp_path, "pytest -q", True) == 2


def test_success_clears_the_count(tmp_path):
    post_tool_review.failure_count(tmp_path, "pytest -q", True)
    assert post_tool_review.failure_count(tmp_path, "pytest -q", False) == 0


def test_whitespace_does_not_make_it_a_different_command(tmp_path):
    post_tool_review.failure_count(tmp_path, "pytest  -q", True)
    assert post_tool_review.failure_count(tmp_path, "pytest -q", True) == 2


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("[]", id="a-list"),
        pytest.param('"x"', id="a-string"),
        pytest.param("42", id="a-number"),
        pytest.param("null", id="null"),
        pytest.param('{"a": 1', id="truncated"),
        pytest.param("", id="empty"),
    ],
)
def test_state_that_is_not_an_object_is_treated_as_absent(tmp_path, contents):
    """The defect: valid JSON of the wrong shape raised AttributeError, uncaught."""
    state_file(tmp_path, contents)

    assert post_tool_review.failure_count(tmp_path, "pytest -q", True) == 1
    assert json.loads(state_file(tmp_path).read_text(encoding="utf-8")) != contents
