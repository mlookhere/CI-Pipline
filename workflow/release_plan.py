"""Plan release PR operations; GitHub MCP must re-verify facts before writing."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[a-f0-9]{40}$")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def mapping(value: Any, name: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{name} must be an object")
    return value


def sha(value: Any, name: str) -> str:
    require(isinstance(value, str) and SHA.fullmatch(value) is not None, f"invalid {name} SHA")
    return value


def labels(value: Any) -> set[str]:
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), "invalid release labels")
    return set(value)


def large_change(diff: dict[str, Any], quality: dict[str, Any]) -> bool:
    files, lines = diff.get("changed_files"), diff.get("changed_lines")
    require(type(files) is int and files > 0, "release diff must include changed files")
    require(type(lines) is int and lines >= 0, "invalid release changed lines")
    return files > quality["max_pr_files"] or lines > quality["max_pr_changed_lines"]


def matching_pr(observation: dict[str, Any], source: str, target: str, issue: int) -> int | None:
    prs = observation.get("open_pull_requests")
    require(isinstance(prs, list), "complete open release PR list is required")
    count = observation.get("open_pull_requests_total")
    require(type(count) is int and count == len(prs), "open release PR list must be fully paginated")
    matches = []
    for entry in prs:
        pr = mapping(entry, "release PR")
        if pr.get("head") != source or pr.get("base") != target:
            continue
        require(type(pr.get("number")) is int and pr["number"] > 0, "release PR number missing")
        require(
            isinstance(pr.get("body"), str) and re.search(rf"\bRefs\s+#{issue}\b", pr["body"]) is not None,
            "matching release PR is not linked to Issue",
        )
        matches.append(pr["number"])
    require(len(matches) <= 1, "multiple open release PRs need reconciliation")
    return matches[0] if matches else None


def required_risks(paths: list[str], config: dict[str, Any]) -> set[str]:
    rules = config["github"]["risk_paths"]
    return {
        label
        for label, patterns in rules.items()
        if any(fnmatch.fnmatch(path, pattern) for pattern in patterns for path in paths)
    }


def release_body(issue: int, dev: str, prod: str, source: str, baseline: str, diff: dict[str, Any]) -> str:
    return (
        f"Refs #{issue}\n\n"
        "## Result\n"
        f"Promote integration {dev} to protected production {prod}.\n\n"
        "## Implementation\n"
        f"Candidate {source}; production baseline {baseline}. "
        f"Diff includes {diff['changed_files']} files and {diff['changed_lines']} changed lines.\n\n"
        "## Verification\n"
        "Require Release metadata, Full regression and production build, and Workflow policy "
        "to complete successfully on the exact PR head. Recheck protection and approvals.\n\n"
        "## Risk\n"
        "Apply every derived risk label to BOTH the release Issue and release PR. "
        "Do not force-push, rename production or bypass protected checks.\n\n"
        "## Remaining work\n"
        "Verify deployment and health after merge. If rollback is needed, create a new "
        f"reviewed revert/release PR into {prod} using {baseline} as the known-good baseline; "
        "never reset a protected branch.\n"
    )


def plan(observed: Any, github: Any, ci: Any) -> dict[str, Any]:
    facts = mapping(observed, "GitHub observation")
    policy = mapping(github, "GitHub policy")
    quality = mapping(ci, "CI policy")["quality"]
    source, target = policy["branches"]["integration"], policy["branches"]["production"]
    require(facts.get("repository") == "mlookhere/CI-Pipline", "repository identity mismatch")
    branches = mapping(facts.get("branches"), "observed branches")
    candidate, baseline = sha(branches.get(source), "integration"), sha(branches.get(target), "production")
    require(candidate != baseline, "no new release candidate")
    require(facts.get("expected_source_sha") == candidate, "stale integration branch SHA")
    require(facts.get("expected_target_sha") == baseline, "stale production branch SHA")
    issue = mapping(facts.get("issue"), "release Issue")
    number = issue.get("number")
    require(type(number) is int and number > 0 and issue.get("state") == "open", "release Issue is not open")
    have = labels(issue.get("labels"))
    require("type:release" in have, "release Issue needs type:release")
    diff = mapping(facts.get("diff"), "release diff")
    paths = diff.get("paths")
    require(isinstance(paths, list) and all(isinstance(p, str) and p for p in paths), "invalid diff paths")
    require(len(paths) == diff.get("changed_files"), "release diff paths are incomplete")
    oversized = large_change(diff, quality)
    need = {"type:release", "risk:deployment", *required_risks(paths, policy)}
    if oversized:
        need.add("risk:large-change")
    existing = matching_pr(facts, source, target, number)
    return {
        "action": "resume_pr" if existing else "propose_pr",
        "pr_number": existing,
        "source": source,
        "target": target,
        "source_sha": candidate,
        "target_sha": baseline,
        "issue": number,
        "issue_labels_to_add": sorted(need - have),
        "pr_labels_required": sorted(need),
        "title": f"[RELEASE] Promote {source} to {target} (Refs #{number})",
        "body": release_body(number, source, target, candidate, baseline, diff),
        "large_change": oversized,
        "merge_authorized": False,
        "release_authorized": False,
        "requires_fresh_github_preconditions": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only release PR planning.")
    parser.add_argument("--observation", type=Path, required=True)
    args = parser.parse_args()
    try:
        facts = json.loads(args.observation.read_text(encoding="utf-8"))
        github = json.loads((ROOT / ".chatfreept/github.json").read_text(encoding="utf-8"))
        ci = json.loads((ROOT / ".chatfreept/ci.json").read_text(encoding="utf-8"))
        result = plan(facts, github, ci)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"release proposal rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
