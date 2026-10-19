"""GitHub handoff replay, failure and collision regressions (Issue #98)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_state as state  # noqa: E402

SHA1 = "a" * 40
SHA2 = "b" * 40


@pytest.fixture
def request():
    return {
        "actor": "session-one",
        "expected_head_sha": SHA1,
        "expected_issue_updated_at": "2026-10-18T10:00:00Z",
        "expected_revision": 0,
        "now": 1000000,
        "lease_seconds": 1800,
        "observed": {
            "repository": "mlookhere/CI-Pipline",
            "issue": {
                "number": 98,
                "state": "open",
                "updated_at": "2026-10-18T10:00:00Z",
                "body": "Original issue description.\n",
            },
            "branch": {"name": "work/98-mcp-state", "sha": SHA1},
            "pull_requests": [],
        },
    }


def test_new_handoff_is_persistable_and_preserves_issue_body(request):
    output = state.plan_handoff(request)
    assert output["action"] == "update_issue"
    assert output["snapshot"]["revision"] == 1
    assert output["snapshot"]["ci"] == "unverified"
    assert output["snapshot"]["phase"] == "developing"
    assert output["issue_body"].startswith("Original issue description.\n")
    assert state.read_handoff(output["issue_body"]) == output["snapshot"]


def test_same_session_replay_is_idempotent(request):
    first = state.plan_handoff(request)
    request["observed"]["issue"]["body"] = first["issue_body"]
    request["expected_revision"] = 1
    second = state.plan_handoff(request)
    assert second["action"] == "unchanged"
    assert second["issue_body"] == first["issue_body"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"repository": "another/repo"}, "wrong repository"),
        ({"branch": {"name": "work/99-other", "sha": SHA1}}, "work branch"),
        ({"branch": {"name": "work/98-mcp-state", "sha": SHA2}}, "stale branch head"),
        ({"pull_requests": None}, "explicit list"),
    ],
)
def test_untrusted_github_observations_fail_closed(request, change, message):
    request["observed"].update(change)
    with pytest.raises(state.Conflict, match=message):
        state.plan_handoff(request)


@pytest.mark.parametrize("field", ["expected_head_sha", "expected_revision", "expected_issue_updated_at"])
def test_stale_precondition_is_rejected(request, field):
    request[field] = "wrong"
    with pytest.raises(state.Conflict, match="stale"):
        state.plan_handoff(request)


def test_active_foreign_lease_refuses_mutation(request):
    first = state.plan_handoff(request)
    request["observed"]["issue"]["body"] = first["issue_body"]
    request["expected_revision"] = 1
    request["actor"] = "session-two"
    with pytest.raises(state.Conflict, match="different session"):
        state.plan_handoff(request)


def test_expired_lease_can_be_recovered(request):
    first = state.plan_handoff(request)
    request["observed"]["issue"]["body"] = first["issue_body"]
    request["expected_revision"] = 1
    request["actor"] = "session-two"
    request["now"] += 1801
    recovered = state.plan_handoff(request)
    assert recovered["snapshot"]["lease"]["holder"] == "session-two"
    assert recovered["snapshot"]["revision"] == 2


def test_duplicate_open_prs_are_not_assumed_unique(request):
    pr = {
        "number": 108,
        "state": "open",
        "merged": False,
        "base": {"ref": "dev"},
        "head": {"ref": "work/98-mcp-state", "sha": SHA1},
        "body": "Refs #98",
    }
    request["observed"]["pull_requests"] = [pr, dict(pr, number=109)]
    with pytest.raises(state.Conflict, match="multiple open PRs"):
        state.plan_handoff(request)


def test_existing_pr_resumed_without_creating_another(request):
    request["observed"]["pull_requests"] = [
        {
            "number": 108,
            "state": "open",
            "merged": False,
            "base": {"ref": "dev"},
            "head": {"ref": "work/98-mcp-state", "sha": SHA1},
            "body": "Refs #98",
        }
    ]
    out = state.plan_handoff(request)
    assert out["snapshot"]["pr"] == 108
    assert out["snapshot"]["phase"] == "review"
    assert out["snapshot"]["ci"] == "unverified"


def test_corrupt_or_duplicate_marker_never_gets_overwritten(request):
    bad = state.START + "\n{bad json}\n" + state.END
    request["observed"]["issue"]["body"] = bad
    with pytest.raises(state.Conflict, match="invalid"):
        state.plan_handoff(request)
    request["observed"]["issue"]["body"] = state.START + state.START + state.END
    with pytest.raises(state.Conflict, match="ambiguous"):
        state.plan_handoff(request)


def test_issue_body_backslashes_and_unicode_round_trip(request):
    request["observed"]["issue"]["body"] = "Windows F:\\\\repo\\\\name — résumé\n"
    result = state.plan_handoff(request)
    assert "Windows F:\\\\repo\\\\name — résumé" in result["issue_body"]
    assert state.read_handoff(result["issue_body"]) == result["snapshot"]


def test_same_session_reentry_with_new_head_requires_new_expected_sha(request):
    first = state.plan_handoff(request)
    request["observed"]["issue"]["body"] = first["issue_body"]
    request["observed"]["branch"]["sha"] = SHA2
    request["expected_revision"] = 1
    with pytest.raises(state.Conflict, match="stale branch head"):
        state.plan_handoff(request)
    request["expected_head_sha"] = SHA2
    assert state.plan_handoff(request)["snapshot"]["head_sha"] == SHA2


def test_saved_state_from_other_issue_is_rejected(request):
    first = state.plan_handoff(request)
    parsed = state.read_handoff(first["issue_body"])
    assert parsed is not None
    parsed["issue"] = 1
    request["observed"]["issue"]["body"] = state.render_handoff("body", parsed)
    request["expected_revision"] = 1
    with pytest.raises(state.Conflict, match="another Issue"):
        state.plan_handoff(request)


def test_invalid_ttl_and_invalid_actor_fail(request):
    request["lease_seconds"] = 28801
    with pytest.raises(state.Conflict, match="TTL"):
        state.plan_handoff(request)
    request["lease_seconds"] = 10
    request["actor"] = ""
    with pytest.raises(state.Conflict, match="actor"):
        state.plan_handoff(request)


def test_cli_uses_only_json_and_exits_on_invalid_input(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["chatfreept_state.py"])
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{not-json"))
    assert state.main() == 1
    assert "handoff rejected:" in capsys.readouterr().err
