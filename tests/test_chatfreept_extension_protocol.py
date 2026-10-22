"""Golden Chat FreePT v0.8.9 browser continuation marker fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_extension_protocol as extension  # noqa: E402

REPO = "mlookhere/CI-Pipline"


def reply(status: str = "Continue", checks: str = "5 required / 5 passed / 0 pending / 0 failed", ci: str = "Passed") -> str:
    fence = chr(96) * 3
    return (
        "Narrative before the last marker.\n\n"
        f"{fence}chatfreept\n"
        f"CHATFREEPT_STATUS: {status}\n"
        "V: 1\nPHASE: Developing\n"
        f"REPO: {REPO}\n"
        "ITEM: 8/9 — regression hardening\n"
        "NOTE: Exact-head verification is still required\n"
        "BRANCH: work/103-extension-contract\n"
        "ISSUE: https://github.com/mlookhere/CI-Pipline/issues/103\n"
        "PR: https://github.com/mlookhere/CI-Pipline/pull/116\n"
        f"CI: {ci}\nCHECKS: {checks}\n"
        "ACTIONS: https://github.com/mlookhere/CI-Pipline/actions/runs/39220075577\n"
        f"{fence}\n"
    )


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("Continue", "resume"),
        ("Needs input", "pause_for_user"),
        ("Plan ready", "begin_development"),
        ("Testing", "pause_for_validation"),
        ("Complete", "audit_before_stop"),
        ("Error", "stop_on_error"),
    ],
)
def test_handoff_statuses_are_never_merge_authority(status, action):
    result = extension.interpret(reply(status), REPO)
    assert result["status"] == status and result["action_hint"] == action
    assert result["merge_authorized"] is False
    assert result["release_authorized"] is False
    assert result["completion_authorized"] is False


def test_model_reported_ci_passed_remains_advisory():
    result = extension.interpret(reply(), REPO)
    assert any("advisory" in warning for warning in result["warnings"])
    assert result["merge_authorized"] is False


@pytest.mark.parametrize("checks", ["0 required / 0 passed / 0 pending / 0 failed", "5 required / 3 passed / 0 pending / 0 failed"])
def test_zero_or_inconsistent_required_checks_fail_closed(checks):
    with pytest.raises(ValueError, match="checks"):
        extension.interpret(reply(checks=checks), REPO)


def test_pending_checks_must_not_be_reported_as_passed():
    with pytest.raises(ValueError, match="reported Passed"):
        extension.interpret(reply(checks="5 required / 2 passed / 3 pending / 0 failed"), REPO)


def test_running_with_pending_is_not_mergeable():
    result = extension.interpret(reply(checks="5 required / 2 passed / 3 pending / 0 failed", ci="Running"), REPO)
    assert result["fields"]["CI"] == "Running"
    assert not result["merge_authorized"]


@pytest.mark.parametrize(
    "text",
    [
        "No status marker",
        "CHATFREEPT_STATUS: Continue\nV: 1\nPHASE: Developing\nREPO: other/repo\nNOTE: no",
        "CHATFREEPT_STATUS: Continue\nV: 2\nPHASE: Developing\nREPO: mlookhere/CI-Pipline\nNOTE: no",
        "CHATFREEPT_STATUS: Continue\nV: 1\nPHASE: Developing\nREPO: mlookhere/CI-Pipline\nNOTE: no\nNOTE: duplicate",
        "CHATFREEPT_STATUS: Continue\nV: 1\nPHASE: Developing\nREPO: mlookhere/CI-Pipline\nNOTE: no\nPR: https://evil.example/pull/1",
        "CHATFREEPT_STATUS: Continue\nV: 1\nPHASE: Developing\nREPO: mlookhere/CI-Pipline\nNOTE: no\nISSUE: https://github.com/other/repo/issues/1",
    ],
)
def test_invalid_or_cross_repo_marker_is_rejected(text):
    with pytest.raises(ValueError):
        extension.interpret(text, REPO)


def test_client_fence_id_and_free_form_checks_are_supported():
    fence = chr(96) * 3
    text = reply(checks="waiting on Actions", ci="Running").replace(f"{fence}chatfreept\n", f'{fence}chatfreept id="card"\n')
    result = extension.interpret(text, REPO)
    assert result["status"] == "Continue"
    assert any("display only" in warning for warning in result["warnings"])


def test_protocol_status_order_matches_extension():
    policy = extension.protocol()
    assert policy["version"] == 1
    assert policy["statuses"] == list(extension.ACTIONS)


def test_repository_lock_must_match_selected_consumer():
    with pytest.raises(ValueError, match="repository lock"):
        extension.interpret(reply(), "mlookhere/another")
