#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_HOOK_EVENTS = {
    "SessionStart",
    "SubagentStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "PostCompact",
    "SubagentStop",
    "Stop",
}
REQUIRED_PR_SECTIONS = {"Issue", "Result", "Implementation", "Verification", "Risk", "Remaining work"}
EXECUTABLES = (
    "flow",
    "ci/run",
    "scripts/bootstrap",
    "scripts/setup-github",
    "scripts/claude-lease",
    "scripts/claude-exec",
    "scripts/validate-workflow",
    "ops/deploy-development",
    "ops/deploy-production",
    "ops/healthcheck",
    "ops/rollback-production",
)


def load_json(path: Path, failures: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
        return {}


def check_python(failures: list[str]) -> None:
    """Compile-check the repository's own Python.

    Scoped to tracked files: walking the working tree pulled in virtualenvs,
    build output, and vendored starter kits, which made this check dominate the
    fast gate (~90s) while reporting on code the repository does not own.
    """
    for relative in sorted(path for path in tracked_files() if path.endswith(".py")):
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (SyntaxError, UnicodeError) as exc:
            failures.append(f"{relative}: invalid Python: {exc}")


def command_exists(command: str) -> bool:
    return (
        subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", f"command -v {command}"], capture_output=True
        ).returncode
        == 0
    )


def check_hooks(hooks_config: Any) -> list[str]:
    """Every lifecycle event wired, pointing at a real hook, with a sane timeout."""
    failures: list[str] = []
    hooks = hooks_config.get("hooks", {}) if isinstance(hooks_config, dict) else {}
    missing_events = REQUIRED_HOOK_EVENTS - set(hooks)
    if missing_events:
        failures.append(
            f".claude/settings.json: missing lifecycle hook events: {', '.join(sorted(missing_events))}"
        )
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            failures.append(f".claude/settings.json: {event} must be a list")
            continue
        for group in groups:
            for hook in group.get("hooks", []):
                failures += check_hook_entry(event, hook)
    return failures


def check_hook_entry(event: str, hook: dict) -> list[str]:
    failures = []
    command = str(hook.get("command", ""))
    match = re.search(r"/\.claude/hooks/([A-Za-z0-9_.-]+)", command)
    if match and not (ROOT / ".claude" / "hooks" / match.group(1)).is_file():
        failures.append(f".claude/settings.json: {event} references missing {match.group(1)}")
    timeout = int(hook.get("timeout", 0) or 0)
    if timeout <= 0 or timeout > 60:
        failures.append(f".claude/settings.json: {event} timeout must be within 1..60 seconds")
    return failures


def check_executables() -> list[str]:
    failures = []
    for relative in EXECUTABLES:
        path = ROOT / relative
        if not path.exists():
            failures.append(f"missing required executable: {relative}")
        elif not os.access(path, os.X_OK):
            failures.append(f"required script is not executable: {relative}")
    return failures


def check_pr_template() -> list[str]:
    pr_template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"(?m)^##\s+(.+?)\s*$", pr_template))
    missing = REQUIRED_PR_SECTIONS - headings
    return [f"PR template is missing sections: {', '.join(sorted(missing))}"] if missing else []


def check_labels(config: dict) -> list[str]:
    failures = []
    state_labels = list(config.get("github", {}).get("state_labels", []))
    if len(state_labels) != len(set(state_labels)):
        failures.append(".claude-workflow.json: duplicate state labels")
    risk_labels = set(config.get("github", {}).get("risk_paths", {}))
    if not all(label.startswith("risk:") for label in risk_labels):
        failures.append(".claude-workflow.json: every changed-path label must start with risk:")
    return failures


def collect_warnings(config: dict) -> list[str]:
    warnings = []
    command_groups = config.get("commands", {})
    for critical in ("unit", "build"):
        if not command_groups.get(critical):
            warnings.append(f"command group {critical!r} is empty; configure it before relying on CI")
    if tracked_migration_paths() and not command_groups.get("migration"):
        warnings.append("migration paths exist but the migration command group is empty")
    if (ROOT / "Dockerfile").exists() and not command_groups.get("image_build"):
        warnings.append("Dockerfile exists but image_build is empty")
    return warnings


def tracked_migration_paths() -> bool:
    """Only tracked paths count: vendored kits and virtualenvs are not this repo's migrations."""
    return any("migrations" in Path(path).parts for path in tracked_files())


def tracked_files() -> list[str]:
    raw = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True).stdout
    return [path for path in raw.decode("utf-8", errors="replace").split("\0") if path]


def check_tracked_artifacts() -> list[str]:
    return [
        f"generated Python artifact must not be committed: {path}"
        for path in tracked_files()
        if path.endswith(".pyc") or "__pycache__" in Path(path).parts
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the repository-owned Claude Code workflow control plane."
    )
    parser.add_argument(
        "--ci", action="store_true", help="Treat portability warnings as CI failures where appropriate."
    )
    args = parser.parse_args()
    failures: list[str] = []
    warnings: list[str] = []

    config_path = ROOT / ".claude-workflow.json"
    config = load_json(config_path, failures)
    hooks_path = ROOT / ".claude" / "settings.json"
    hooks_config = load_json(hooks_path, failures)

    if int(config.get("version", 0)) < 2:
        failures.append(".claude-workflow.json: expected workflow schema version 2 or newer")
    for branch_kind in ("integration", "production"):
        value = str(config.get("branches", {}).get(branch_kind, ""))
        if not value:
            failures.append(f".claude-workflow.json: missing {branch_kind} branch")
        elif (
            subprocess.run(
                ["git", "check-ref-format", "--branch", value], cwd=ROOT, capture_output=True
            ).returncode
            != 0
        ):
            failures.append(f".claude-workflow.json: invalid {branch_kind} branch {value!r}")

    failures += check_hooks(hooks_config)
    failures += check_executables()
    failures += check_pr_template()
    failures += check_labels(config)
    warnings += collect_warnings(config)
    failures += check_tracked_artifacts()

    check_python(failures)

    policy = subprocess.run(
        [sys.executable, str(ROOT / "workflow" / "check_workflow_policy.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if policy.returncode != 0:
        failures.extend(line for line in policy.stdout.splitlines() if line.startswith("failure:"))

    if args.ci and not command_exists("git"):
        failures.append("git is required in CI")

    for warning in warnings:
        print(f"warning: {warning}")
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        print(f"Claude Code workflow self-test failed with {len(failures)} finding(s).")
        return 1
    print(f"Claude Code workflow self-test passed with {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
