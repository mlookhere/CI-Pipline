"""Release PR planning is deterministic and never confers protected-branch approval."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import release_plan as release  # noqa: E402

GITHUB = json.loads((ROOT / ".chatfreept/github.json").read_text(encoding="utf-8"))
CI = json.loads((ROOT / ".chatfreept/ci.json").read_text(encoding="utf-8"))


@pytest.fixture
def facts():
    return {
        "repository": "mlookhere/CI-Pipline",
        "branches": {"dev": "a" * 40, "master": "b" * 40},
        "expected_source_sha": "a" * 40,
        "expected_target_sha": "b" * 40,
        "issue": {"number": 120, "state": "open", "labels": ["type:release"]},
        "diff": {
            "changed_files": 2,
            "changed_lines": 30,
            "paths": ["workflow/release_manifest.py", "README.md"],
        },
        "open_pull_requests": [],
    }


def test_small_release_labels_both_objects_without_approval(facts):
    result = release.plan(facts, GITHUB, CI)
    assert result["action"] == "propose_pr"
    assert result["source"] == "dev" and result["target"] == "master"
    assert result["issue_labels_to_add"] == ["risk:ci", "risk:deployment"]
    assert "risk:large-change" not in result["pr_labels_required"]
    assert "Refs #120" in result["body"]
    assert "known-good baseline" in result["body"]
    assert result["merge_authorized"] is False and result["release_authorized"] is False


@pytest.mark.parametrize(("files", "lines"), [(81, 120), (2, 4001)])
def test_oversized_release_requires_both_large_change_labels(facts, files, lines):
    facts["diff"] = {
        "changed_files": files,
        "changed_lines": lines,
        "paths": [f"docs/file-{index}.md" for index in range(files)],
    }
    result = release.plan(facts, GITHUB, CI)
    assert "risk:large-change" in result["issue_labels_to_add"]
    assert "risk:large-change" in result["pr_labels_required"]


def test_existing_large_issue_label_is_not_duplicated(facts):
    facts["diff"]["changed_lines"] = 5001
    facts["issue"]["labels"].append("risk:large-change")
    result = release.plan(facts, GITHUB, CI)
    assert "risk:large-change" not in result["issue_labels_to_add"]
    assert "risk:large-change" in result["pr_labels_required"]


def test_existing_release_pr_is_resumed(facts):
    facts["open_pull_requests"] = [{"number": 141, "head": "dev", "base": "master", "body": "Refs #120"}]
    result = release.plan(facts, GITHUB, CI)
    assert result["action"] == "resume_pr" and result["pr_number"] == 141


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda value: value.update(repository="another/repo"), "repository identity"),
        (lambda value: value.update(expected_source_sha="c" * 40), "stale integration"),
        (lambda value: value.update(expected_target_sha="c" * 40), "stale production"),
        (lambda value: value["issue"].update(state="closed"), "not open"),
        (lambda value: value["issue"].update(labels=[]), "type:release"),
        (lambda value: value.update(open_pull_requests=None), "complete open"),
        (lambda value: value["diff"].update(paths=[]), "incomplete"),
    ],
)
def test_missing_or_stale_release_evidence_fails_closed(facts, mutate, error):
    mutate(facts)
    with pytest.raises(ValueError, match=error):
        release.plan(facts, GITHUB, CI)


def test_ambiguous_release_prs_are_rejected(facts):
    row = {"number": 142, "head": "dev", "base": "master", "body": "Refs #120"}
    facts["open_pull_requests"] = [row, dict(row, number=143)]
    with pytest.raises(ValueError, match="multiple open"):
        release.plan(facts, GITHUB, CI)


def test_existing_wrong_issue_release_pr_is_not_duplicated(facts):
    facts["open_pull_requests"] = [{"number": 142, "head": "dev", "base": "master", "body": "Refs #999"}]
    with pytest.raises(ValueError, match="not linked"):
        release.plan(facts, GITHUB, CI)


def test_invalid_candidate_sha_and_identical_branches_fail(facts):
    facts["branches"]["dev"] = "bad"
    with pytest.raises(ValueError, match="SHA"):
        release.plan(facts, GITHUB, CI)
    facts["branches"]["dev"] = facts["branches"]["master"]
    facts["expected_source_sha"] = facts["branches"]["master"]
    with pytest.raises(ValueError, match="no new"):
        release.plan(facts, GITHUB, CI)
