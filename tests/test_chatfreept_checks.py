"""Check provenance must never equate stale, missing, or skipped gates with green (#99)."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import chatfreept_checks as checks  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40
PROJECT = json.loads((ROOT / ".chatfreept" / "project.json").read_text(encoding="utf-8"))


def evidence(kind: str = "integration", event: str = "pull_request") -> dict:
    branch = PROJECT["branches"][kind]
    names = PROJECT["required_checks"][kind]
    runs = [
        {"id": i + 1, "name": name, "head_sha": HEAD, "event": event, "status": "completed", "conclusion": "success"}
        for i, name in enumerate(names)
    ]
    jobs = [
        {"id": i + 10, "name": name, "head_sha": HEAD, "run_id": i + 1, "status": "completed", "conclusion": "success"}
        for i, name in enumerate(names)
    ]
    return {
        "repository": "mlookhere/CI-Pipline",
        "base_branch": branch,
        "head_sha": HEAD,
        "event_head_sha": HEAD,
        "observed_head_sha": HEAD,
        "event": event,
        "protection": {
            "branch": branch,
            "protected": True,
            "required_status_checks": {"contexts": names, "checks": []},
        },
        "workflow_runs": {"total_count": len(runs), "workflow_runs": runs},
        "jobs": {"total_count": len(jobs), "jobs": jobs},
    }


@pytest.mark.parametrize("kind", ["integration", "production"])
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_every_required_job_succeeds_on_exact_head_and_protected_branch(kind, event):
    assert checks.reconcile(evidence(kind, event), PROJECT)["mergeable"] is True


@pytest.mark.parametrize(
    ("field", "value", "part"),
    [
        ("repository", "other/repo", "another repository"),
        ("observed_head_sha", OLD, "stale"),
        ("event_head_sha", OLD, "event head"),
        ("event", "push", "unsupported event"),
    ],
)
def test_bad_scope_is_rejected(field, value, part):
    facts = evidence()
    facts[field] = value
    assert any(part in x for x in checks.reconcile(facts, PROJECT)["failures"])


def test_no_check_runs_is_not_green():
    facts = evidence()
    facts["workflow_runs"] = {"total_count": 0, "workflow_runs": []}
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


def test_missing_required_job_blocks_merge():
    facts = evidence()
    facts["jobs"]["jobs"].pop()
    facts["jobs"]["total_count"] -= 1
    assert "absent" in " ".join(checks.reconcile(facts, PROJECT)["failures"])


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_bad_required_job_conclusion_or_status_blocks(status, conclusion):
    facts = evidence()
    facts["jobs"]["jobs"][0].update(status=status, conclusion=conclusion)
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_bad_required_workflow_run_blocks(conclusion):
    facts = evidence()
    facts["workflow_runs"]["workflow_runs"][0]["conclusion"] = conclusion
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


def test_stale_job_cannot_borrow_current_run_success():
    facts = evidence()
    facts["jobs"]["jobs"][0]["head_sha"] = OLD
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


def test_ambiguous_duplicate_named_job_blocks_merge():
    facts = evidence()
    facts["jobs"]["jobs"].append(dict(facts["jobs"]["jobs"][0], id=100))
    facts["jobs"]["total_count"] += 1
    assert "ambiguous" in " ".join(checks.reconcile(facts, PROJECT)["failures"])


def test_partial_pagination_is_invalid_not_empty_success():
    facts = evidence()
    facts["jobs"]["total_count"] += 1
    assert "fully paginated" in " ".join(checks.reconcile(facts, PROJECT)["failures"])


@pytest.mark.parametrize("mutation", ["missing", "unprotected", "different"])
def test_live_protection_must_match_project(mutation):
    facts = evidence()
    if mutation == "missing":
        del facts["protection"]["required_status_checks"]
    elif mutation == "unprotected":
        facts["protection"]["protected"] = False
    else:
        facts["protection"]["required_status_checks"]["contexts"] = ["PR metadata"]
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


def test_skipped_optional_security_job_is_neutral_only_when_explicitly_optional():
    facts = evidence()
    facts["jobs"]["jobs"].append(
        {"name": "Dependency review", "run_id": 1, "head_sha": HEAD, "status": "completed", "conclusion": "skipped"}
    )
    facts["jobs"]["total_count"] += 1
    assert checks.reconcile(facts, PROJECT)["mergeable"] is True
    facts["jobs"]["jobs"][-1]["name"] = "Unexpected scanner"
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


def test_missing_evidence_is_not_success():
    assert checks.reconcile({}, PROJECT)["mergeable"] is False
    assert checks.reconcile(None, PROJECT)["mergeable"] is False


def test_conflicting_or_missing_branch_identity_blocks():
    facts = evidence()
    facts["base_branch"] = "main"
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False


def test_copying_earlier_success_does_not_pass_a_new_head():
    facts = copy.deepcopy(evidence())
    facts["head_sha"] = OLD
    facts["observed_head_sha"] = OLD
    facts["event_head_sha"] = OLD
    assert checks.reconcile(facts, PROJECT)["mergeable"] is False
