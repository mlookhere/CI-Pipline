"""Causal CI diagnostics are bounded, replay-safe, and always non-authoritative (#100)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_diagnostics as diag  # noqa: E402

HEAD = "a" * 40


@pytest.fixture
def observation():
    return {
        "head_sha": HEAD,
        "run": {
            "id": 103,
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/mlookhere/CI-Pipline/actions/runs/103",
        },
        "jobs": {
            "total_count": 1,
            "jobs": [
                {
                    "id": 24,
                    "run_id": 103,
                    "head_sha": HEAD,
                    "name": "Fast deterministic gates",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "https://github.com/mlookhere/CI-Pipline/actions/jobs/24",
                    "steps": [{"number": 2, "name": "Run ./ci/run fast", "conclusion": "failure"}],
                }
            ],
        },
        "logs": {"24": "2026-10-19T06:00:00Z ERROR failed the unit tests\n"},
    }


def test_first_failure_has_exact_provenance_and_no_green_claim(observation):
    result = diag.diagnose(observation)
    assert result["status"] == "failed"
    assert result["ci_verified"] is False
    assert result["run_id"] == 103 and result["job_id"] == 24
    assert result["step"] == "Run ./ci/run fast"
    assert result["attempts"]["state"]["failed_fixes"] == 0


def test_pending_does_not_count_as_passed(observation):
    observation["run"]["status"] = "in_progress"
    observation["jobs"]["jobs"][0]["status"] = "queued"
    observation["jobs"]["jobs"][0]["conclusion"] = None
    result = diag.diagnose(observation)
    assert result["status"] == "pending" and result["ci_verified"] is False


@pytest.mark.parametrize("error", ["HTTP 503", "rate limit reached", "runner lost"])
def test_possible_infrastructure_failure_is_not_automatically_retried(observation, error):
    observation["logs"]["24"] = f"ERROR: {error}"
    result = diag.diagnose(observation)
    assert result["classification"] == "possible-infrastructure"
    assert result["status"] == "failed"


def test_log_secrets_and_ansi_are_redacted(observation):
    secret = "ghp_" + "x" * 24
    observation["logs"]["24"] = f"\x1b[31mERROR token={secret}\x1b[0m"
    content = diag.diagnose(observation)["log_excerpt"]
    assert secret not in content
    assert "[REDACTED]" in content and "\x1b" not in content


def test_bounded_tail_does_not_include_older_lines(observation):
    observation["logs"]["24"] = "old line\n" * 200 + "ERROR latest\n"
    result = diag.diagnose(observation)
    assert "old line\n" * 100 not in result["log_excerpt"]
    assert len(result["log_excerpt"]) <= diag.MAX_CHARS


def test_missing_logs_cannot_invent_a_cause(observation):
    observation["logs"] = {}
    assert diag.diagnose(observation)["status"] == "missing-logs"


def test_incomplete_job_pagination_blocks_diagnostics(observation):
    observation["jobs"]["total_count"] = 2
    assert diag.diagnose(observation)["status"] == "invalid-evidence"


def test_stale_job_head_is_invalid(observation):
    observation["jobs"]["jobs"][0]["head_sha"] = "b" * 40
    assert diag.diagnose(observation)["status"] == "invalid-evidence"


def test_missing_job_collection_is_invalid(observation):
    observation["jobs"]["jobs"] = []
    observation["jobs"]["total_count"] = 0
    assert diag.diagnose(observation)["status"] == "invalid-evidence"


def test_three_failed_fixes_require_human_input(observation):
    first = diag.diagnose(observation)
    fingerprint = first["fingerprint"]
    observation["attempts"] = {fingerprint: {"failed_fixes": 2, "last_head": "b" * 40}}
    observation["fix_applied"] = True
    result = diag.diagnose(observation)
    assert result["attempts"]["human_input_required"] is True
    assert result["attempts"]["state"]["failed_fixes"] == 3


def test_same_head_cannot_increment_twice(observation):
    fingerprint = diag.diagnose(observation)["fingerprint"]
    observation["attempts"] = {fingerprint: {"failed_fixes": 1, "last_head": HEAD}}
    observation["fix_applied"] = True
    assert diag.diagnose(observation)["status"] == "invalid-evidence"


def test_unavailable_run_metadata_fails_closed(observation):
    del observation["run"]["html_url"]
    assert diag.diagnose(observation)["status"] == "invalid-evidence"


def test_rerun_same_error_has_stable_fingerprint(observation):
    original = diag.diagnose(observation)["fingerprint"]
    observation["run"]["id"] = 104
    observation["run"]["html_url"] = "https://github.com/mlookhere/CI-Pipline/actions/runs/104"
    observation["jobs"]["jobs"][0]["run_id"] = 104
    assert diag.diagnose(observation)["fingerprint"] == original
