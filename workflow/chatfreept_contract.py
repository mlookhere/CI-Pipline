#!/usr/bin/env python3
"""Fail-closed validation of the ChatGPT project contract (Issue #96).

GitHub branch-protection settings still require live verification before a merge.
This checker prevents local metadata and declared workflow jobs from silently drifting.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "integration": (".github/workflows/ci-pr.yml", ".github/workflows/security.yml"),
    "production": (".github/workflows/ci-release.yml", ".github/workflows/security.yml"),
}
STAGES = ("fast", "pr", "release", "nightly", "audit")
PR_SECTIONS = ("Result", "Implementation", "Verification", "Risk", "Remaining work")


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def workflow_jobs(source: str) -> set[str]:
    """Read job-level names, not step names, from the current YAML layout."""
    match = re.search(r"(?m)^jobs:\s*$", source)
    if match is None:
        return set()
    return {
        name.strip().strip('"').strip("'")
        for name in re.findall(r"(?m)^    name:\s*([^\r\n#]+)", source[match.end() :])
    }


def check_identity(project: dict[str, Any], legacy: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    github = obj(legacy.get("github"))
    legacy_branches = obj(legacy.get("branches"))
    branches = obj(project.get("branches"))
    expected_repo = f"{github.get('expected_owner', '')}/{github.get('expected_repository', '')}"
    if not nonempty(project.get("repository")) or project["repository"] != expected_repo:
        failures.append("project.json: repository identity disagrees with the CI repository")
    if not nonempty(project.get("name")):
        failures.append("project.json: name must be nonempty")
    if project.get("status") not in ("planning", "developing", "testing", "complete"):
        failures.append("project.json: invalid delivery status")
    for kind in ("integration", "production"):
        value = branches.get(kind)
        if not nonempty(value) or value != legacy_branches.get(kind):
            failures.append(f"project.json: {kind} branch differs from the active CI configuration")
    if branches.get("default") != branches.get("integration"):
        failures.append("project.json: default must remain the integration branch")
    if not nonempty(branches.get("requested_production")):
        failures.append("project.json: requested production branch must be explicit")
    if type(branches.get("rename_pending_verification")) is not bool:
        failures.append("project.json: rename_pending_verification must be a boolean")
    return failures


def check_agent(project: dict[str, Any]) -> list[str]:
    agent = obj(project.get("agent"))
    required = {
        "provider": "chatgpt",
        "tool": "github-mcp",
        "exclusive": True,
        "github_source_of_truth": True,
        "background_worker": False,
    }
    return [
        f"project.json: agent.{name} must be {expected!r}"
        for name, expected in required.items()
        if agent.get(name) != expected or type(agent.get(name)) is not type(expected)
    ]


def check_tracking(project: dict[str, Any], legacy: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    tracking = obj(project.get("tracking"))
    for key in ("control_issue", "planning_issue", "max_failed_fixes_per_cause"):
        value = tracking.get(key)
        if type(value) is not int or value < 1:
            failures.append(f"project.json: tracking.{key} must be a positive integer")
    if tracking.get("work_prefix") != obj(legacy.get("worktrees")).get("branch_prefix"):
        failures.append("project.json: work prefix differs from the active CI configuration")
    if tracking.get("required_pr_sections") != list(PR_SECTIONS):
        failures.append("project.json: required PR sections disagree with the existing PR gate")
    return failures


def check_job_definitions(kind: str, names: list[str], workflows: dict[str, str]) -> list[str]:
    failures: list[str] = []
    available: set[str] = set()
    for path in WORKFLOWS[kind]:
        source = workflows.get(path)
        if source is None:
            failures.append(f"project.json: missing workflow {path}")
        else:
            available.update(workflow_jobs(source))
    for name in names:
        if name not in available:
            failures.append(f"project.json: {kind} required job {name!r} has no workflow definition")
    return failures


def check_required_checks(
    project: dict[str, Any], legacy: dict[str, Any], workflows: dict[str, str]
) -> list[str]:
    failures: list[str] = []
    declared = obj(project.get("required_checks"))
    protected = obj(obj(legacy.get("github")).get("branch_protection"))
    for kind in WORKFLOWS:
        raw = declared.get(kind)
        names = raw if isinstance(raw, list) else []
        if not names or any(not nonempty(name) for name in names) or len(set(map(str, names))) != len(names):
            failures.append(f"project.json: {kind} required checks must be nonempty and unique")
            continue
        expected = obj(protected.get(kind)).get("required_checks")
        if names != expected:
            failures.append(f"project.json: {kind} required checks differ from active protection config")
        failures.extend(check_job_definitions(kind, names, workflows))
    if declared.get("require_completed") is not True or declared.get("zero_is_success") is not False:
        failures.append("project.json: required checks must fail closed")
    return failures


def check_stage_entries(stage: str, selected: Any, commands: dict[str, Any]) -> list[str]:
    if not isinstance(selected, list) or not selected:
        return [f"project.json: {stage} stage has no configured command groups"]
    failures: list[str] = []
    for group in selected:
        entries = commands.get(group)
        if group != "quality" and (
            not isinstance(entries, list) or not entries or any(not nonempty(cmd) for cmd in entries)
        ):
            failures.append(f"project.json: {stage} references empty or missing group {group!r}")
    return failures


def check_commands(project: dict[str, Any], legacy: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    declared = obj(project.get("commands"))
    if declared.get("bootstrap") != "./scripts/bootstrap --ci":
        failures.append("project.json: bootstrap must invoke the headless CI entry point")
    stages = obj(legacy.get("stages"))
    groups = obj(legacy.get("commands"))
    for stage in STAGES:
        if declared.get(stage) != f"./ci/run {stage}":
            failures.append(f"project.json: {stage} must invoke ./ci/run {stage}")
        failures.extend(check_stage_entries(stage, stages.get(stage), groups))
    return failures


def check_slices(project: dict[str, Any]) -> list[str]:
    slices = project.get("slices")
    if not isinstance(slices, list) or [obj(s).get("number") for s in slices] != list(range(1, 10)):
        return ["project.json: slices must be numbered 1 through 9 in order"]
    failures: list[str] = []
    ids = [obj(item).get("issue") for item in slices]
    if any(type(issue) is not int or issue < 1 for issue in ids) or len(set(ids)) != len(ids):
        failures.append("project.json: slice Issues must be unique positive integers")
    if any(obj(item).get("status") not in ("ready", "active", "complete") for item in slices):
        failures.append("project.json: invalid slice status")
    return failures


def check_project(project: Any, legacy: Any, workflows: dict[str, str]) -> list[str]:
    if not isinstance(project, dict) or not isinstance(legacy, dict):
        return ["ChatGPT manifest and legacy CI config must be JSON objects"]
    failures: list[str] = []
    if type(project.get("version")) is not int or project["version"] != 1:
        failures.append("project.json: unsupported schema version (expected 1)")
    failures.extend(check_identity(project, legacy))
    failures.extend(check_agent(project))
    failures.extend(check_tracking(project, legacy))
    failures.extend(check_required_checks(project, legacy, workflows))
    failures.extend(check_commands(project, legacy))
    failures.extend(check_slices(project))
    return failures


def validate_repository(root: Path = ROOT) -> list[str]:
    """Read all inputs; missing files and invalid JSON are errors, never empty defaults."""

    def read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    try:
        project = read_json(root / ".chatfreept" / "project.json")
        legacy = read_json(root / ".chatfreept" / "github.json")
        stages = read_json(root / ".chatfreept" / "ci.json")
    except (OSError, UnicodeError, ValueError) as exc:
        return [f"ChatGPT project metadata could not be read: {exc}"]
    workflow_texts: dict[str, str] = {}
    for paths in WORKFLOWS.values():
        for path in paths:
            try:
                workflow_texts[path] = (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                pass
    failures = check_project(project, legacy, workflow_texts)
    if not isinstance(legacy, dict) or not isinstance(stages, dict):
        return ["ChatGPT policy and CI stages must be JSON objects"]
    for key in ("commands", "stages"):
        if legacy.get(key) != stages.get(key):
            failures.append(f"ChatGPT policy {key} differs from headless CI stages")
    return failures


def main() -> int:
    failures = validate_repository()
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        print(f"ChatGPT project self-test failed with {len(failures)} finding(s).")
        return 1
    print("ChatGPT project self-test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
