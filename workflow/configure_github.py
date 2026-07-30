#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".claude-workflow.json"
API_VERSION = "2026-03-10"


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"unable to read {CONFIG_PATH}: {exc}")


def gh_api(endpoint: str, payload: dict[str, Any], *, method: str = "PUT", dry_run: bool) -> None:
    print(f"{method} {endpoint}")
    if dry_run:
        print(json.dumps(payload, indent=2))
        return
    result = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {API_VERSION}",
            endpoint,
            "--input",
            "-",
        ],
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)


def gh_output(endpoint: str, jq: str = "") -> str:
    command = [
        "gh",
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {API_VERSION}",
        endpoint,
    ]
    if jq:
        command.extend(["--jq", jq])
    result = subprocess.run(
        command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def verify_bootstrap(branch: str, *, dry_run: bool) -> None:
    required = [
        ".claude-workflow.json",
        "workflow/claude_flow.py",
        ".github/workflows/ci-pr.yml",
        ".github/workflows/ci-release.yml",
        ".github/workflows/security.yml",
        "workflow/check_workflow_policy.py",
    ]
    if dry_run:
        print(f"VERIFY workflow bootstrap files on {branch}: {', '.join(required)}")
        return
    missing = [
        path
        for path in required
        if not gh_output(f"repos/{{owner}}/{{repo}}/contents/{path}?ref={quote(branch, safe='')}", ".path")
    ]
    if missing:
        fail(
            f"the production branch {branch!r} does not yet contain the workflow bootstrap files: "
            + ", ".join(missing)
            + ". Merge the starter-kit bootstrap commit first, then rerun setup."
        )
    print(f"verified workflow bootstrap on: {branch}")


def ensure_branch(branch: str, source_branch: str | None, *, dry_run: bool) -> None:
    encoded = quote(branch, safe="")
    if not dry_run and gh_output(f"repos/{{owner}}/{{repo}}/branches/{encoded}", ".name"):
        print(f"branch exists: {branch}")
        return
    if dry_run:
        print(f"ENSURE branch {branch} from {source_branch or 'repository default branch'}")
        return
    source = source_branch or gh_output("repos/{owner}/{repo}", ".default_branch")
    if not source:
        fail("unable to determine the repository default branch; push an initial commit first")
    source_sha = gh_output(f"repos/{{owner}}/{{repo}}/branches/{quote(source, safe='')}", ".commit.sha")
    if not source_sha:
        fail(f"unable to resolve source branch {source!r} while creating {branch!r}")
    gh_api(
        "repos/{owner}/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": source_sha},
        method="POST",
        dry_run=False,
    )
    print(f"created branch: {branch} from {source}")


def branch_payload(contexts: list[str], reviews: int) -> dict[str, Any]:
    return {
        "required_status_checks": {"strict": True, "contexts": contexts},
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": reviews > 0,
            "require_code_owner_reviews": False,
            "required_approving_review_count": reviews,
            "require_last_push_approval": reviews > 0,
        },
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_conversation_resolution": True,
        "lock_branch": False,
        "allow_fork_syncing": False,
    }


def configure_branch(branch: str, contexts: list[str], reviews: int, *, dry_run: bool) -> None:
    encoded = quote(branch, safe="")
    gh_api(
        f"repos/{{owner}}/{{repo}}/branches/{encoded}/protection",
        branch_payload(contexts, reviews),
        dry_run=dry_run,
    )
    print(f"configured branch protection: {branch}")


def configure_environment(name: str, *, dry_run: bool) -> None:
    payload = {
        "wait_timer": 0,
        "prevent_self_review": False,
        "deployment_branch_policy": {
            "protected_branches": True,
            "custom_branch_policies": False,
        },
    }
    gh_api(f"repos/{{owner}}/{{repo}}/environments/{quote(name, safe='')}", payload, dry_run=dry_run)
    print(f"configured environment: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure standard GitHub branch protection and deployment environments."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = load_config()
    github = config.get("github", {})
    expected_owner = str(github.get("expected_owner") or "").strip()
    expected_repository = str(github.get("expected_repository") or "").strip()
    actual_owner = gh_output("repos/{owner}/{repo}", ".owner.login")
    actual_repository = gh_output("repos/{owner}/{repo}", ".name")
    if (
        expected_owner
        and actual_owner
        and actual_owner.lower() != expected_owner.lower()
        and not os.environ.get("ALLOW_OTHER_GITHUB_OWNER")
    ):
        fail(
            f"repository owner {actual_owner!r} does not match expected owner {expected_owner!r}; set ALLOW_OTHER_GITHUB_OWNER=1 only intentionally"
        )
    if expected_repository and actual_repository and actual_repository.lower() != expected_repository.lower():
        fail(
            f"repository {actual_repository!r} does not match initialized repository {expected_repository!r}; rerun claude-project init in the correct repository"
        )
    protection = github.get("branch_protection", {})
    branches = config["branches"]

    integration = protection.get("integration", {})
    production = protection.get("production", {})
    ensure_branch(branches["production"], None, dry_run=args.dry_run)
    verify_bootstrap(branches["production"], dry_run=args.dry_run)
    ensure_branch(branches["integration"], branches["production"], dry_run=args.dry_run)
    configure_branch(
        branches["integration"],
        list(integration.get("required_checks", [])),
        int(integration.get("required_approving_reviews", 0)),
        dry_run=args.dry_run,
    )
    configure_branch(
        branches["production"],
        list(production.get("required_checks", [])),
        int(production.get("required_approving_reviews", 0)),
        dry_run=args.dry_run,
    )
    for environment in ["development", "production"]:
        configure_environment(environment, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
