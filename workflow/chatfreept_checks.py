"""Fail-closed reconciliation of GitHub branch protection and Actions evidence (#99).

Inputs must be freshly read from GitHub MCP. This is an offline verifier, not
an Actions job or a substitute for GitHub's own protected-branch enforcement.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[a-f0-9]{40}$")
EVENTS = {"pull_request", "merge_group"}
OPTIONAL_SKIPPED = {"Dependency review", "CodeQL for Actions", "Optional Claude advisory review"}


def record(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def collection(value: Any, key: str) -> list[dict[str, Any]]:
    source = record(value, key)
    rows = source.get(key)
    count = source.get("total_count")
    if not isinstance(rows, list) or type(count) is not int or count != len(rows):
        raise ValueError(f"{key} must be fully paginated with an accurate total_count")
    return [record(row, key) for row in rows]


def protected_names(protection: dict[str, Any]) -> list[str]:
    rule = record(protection.get("required_status_checks"), "required_status_checks")
    contexts = rule.get("contexts", [])
    checks = rule.get("checks", [])
    if not isinstance(contexts, list) or not isinstance(checks, list):
        raise ValueError("branch protection required contexts/checks are missing")
    names = contexts + [record(check, "required check").get("context") for check in checks]
    if not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("branch protection declares zero or malformed required checks")
    if len(set(names)) != len(names):
        raise ValueError("duplicate required check names in branch protection")
    return names


def check_protection(evidence: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    branch = evidence.get("base_branch")
    configured = record(expected.get("branches"), "project branches")
    kind = "integration" if branch == configured.get("integration") else "production"
    if branch not in (configured.get("integration"), configured.get("production")):
        return ["base branch is not configured integration or production"]
    declared = record(expected.get("required_checks"), "project required_checks").get(kind)
    if not isinstance(declared, list) or not declared:
        return ["project required checks are empty or missing"]
    protection = record(evidence.get("protection"), "live branch protection")
    if protection.get("branch") != branch or protection.get("protected") is not True:
        failures.append("live protected branch identity or protection is missing")
    actual = protected_names(protection)
    if set(actual) != set(declared) or len(actual) != len(declared):
        failures.append("live required checks differ from checked-in project policy")
    return failures


def check_event(evidence: dict[str, Any]) -> list[str]:
    event = evidence.get("event")
    head = evidence.get("head_sha")
    observed = evidence.get("observed_head_sha")
    if event not in EVENTS:
        return ["unsupported event: expected pull_request or merge_group"]
    if not isinstance(head, str) or SHA.fullmatch(head) is None or observed != head:
        return ["stale or missing exact head SHA"]
    if evidence.get("event_head_sha") != head:
        return ["event head SHA differs from the current checked head"]
    if evidence.get("repository") != "mlookhere/CI-Pipline":
        return ["GitHub observation belongs to another repository"]
    return []


def check_runs(evidence: dict[str, Any], head: str) -> tuple[list[str], dict[int, dict[str, Any]]]:
    failures: list[str] = []
    runs = collection(evidence.get("workflow_runs"), "workflow_runs")
    seen: dict[int, dict[str, Any]] = {}
    matching = 0
    for run in runs:
        if run.get("head_sha") != head:
            continue
        matching += 1
        identifier = run.get("id")
        if type(identifier) is not int or identifier < 1 or identifier in seen:
            failures.append("missing or duplicate workflow run ID")
            continue
        seen[identifier] = run
        if run.get("event") != evidence["event"]:
            failures.append(f"run {identifier} is not for the selected event")
        if run.get("status") != "completed":
            failures.append(f"run {identifier} is pending")
        if run.get("conclusion") != "success":
            if not (run.get("conclusion") == "skipped" and run.get("name") in OPTIONAL_SKIPPED):
                failures.append(f"run {identifier} is not successful")
    if matching == 0:
        failures.append("no workflow runs for exact head SHA")
    return failures, seen


def check_jobs(evidence: dict[str, Any], runs: dict[int, dict[str, Any]], names: list[str]) -> list[str]:
    failures: list[str] = []
    jobs = collection(evidence.get("jobs"), "jobs")
    required: dict[str, int] = {}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str):
            failures.append("job name is missing")
            continue
        run = runs.get(job.get("run_id"))
        if run is None or job.get("head_sha") != evidence["head_sha"]:
            failures.append(f"job {name!r} lacks a matching exact-head workflow run")
            continue
        if name in names:
            required[name] = required.get(name, 0) + 1
        elif job.get("conclusion") == "skipped" and name in OPTIONAL_SKIPPED:
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            failures.append(f"job {name!r} has not successfully completed")
    for name in names:
        if required.get(name, 0) != 1:
            failures.append(f"required job {name!r} is absent or ambiguous")
    return failures


def reconcile(evidence: Any, project: Any) -> dict[str, Any]:
    try:
        observed = record(evidence, "evidence")
        expected = record(project, "project")
        failures = check_event(observed)
        if failures:
            return {"mergeable": False, "failures": failures, "head_sha": observed.get("head_sha")}
        failures.extend(check_protection(observed, expected))
        names = protected_names(record(observed.get("protection"), "live protection"))
        run_failures, runs = check_runs(observed, observed["head_sha"])
        failures.extend(run_failures)
        failures.extend(check_jobs(observed, runs, names))
    except (ValueError, TypeError) as exc:
        failures = [str(exc)]
    return {
        "mergeable": not failures,
        "failures": failures,
        "head_sha": evidence.get("head_sha") if isinstance(evidence, dict) else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile live GitHub check evidence; default is deny.")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=ROOT / ".chatfreept" / "project.json")
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        project = json.loads(args.project.read_text(encoding="utf-8"))
        result = reconcile(evidence, project)
    except (OSError, UnicodeError, ValueError) as exc:
        result = {"mergeable": False, "failures": [f"cannot read evidence: {exc}"]}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["mergeable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
