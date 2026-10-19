"""Validate restart-safe ChatGPT handoffs against fresh GitHub observations.

This module plans an Issue body update; only the authorized GitHub MCP client
performs the write after rechecking preconditions. No network or agent runtime.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

START = "<!-- chatfreept-handoff:start -->"
END = "<!-- chatfreept-handoff:end -->"
SHA = re.compile(r"^[0-9a-f]{40}$")
OWNER = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
MAX_LEASE_SECONDS = 28800


class Conflict(ValueError):
    """A missing, ambiguous, or stale GitHub fact must stop the mutation."""


def require(condition: bool, explanation: str) -> None:
    if not condition:
        raise Conflict(explanation)


def mapping(value: Any, name: str) -> dict[str, Any]:
    require(isinstance(value, dict), f"{name} must be an object")
    return value


def positive(value: Any) -> bool:
    return type(value) is int and value > 0


def read_handoff(body: str) -> dict[str, Any] | None:
    require(isinstance(body, str) and len(body) <= 200000, "Issue body is invalid or too large")
    if START not in body and END not in body:
        return None
    require(body.count(START) == 1 and body.count(END) == 1, "ambiguous handoff markers")
    begin = body.index(START) + len(START)
    finish = body.index(END)
    require(begin < finish, "handoff markers are reversed")
    try:
        value = json.loads(body[begin:finish].strip())
    except ValueError as exc:
        raise Conflict(f"handoff JSON is invalid: {exc}") from exc
    state = mapping(value, "handoff")
    require(state.get("version") == 1 and positive(state.get("revision")), "invalid handoff version")
    require(
        isinstance(state.get("head_sha"), str) and SHA.fullmatch(state["head_sha"]) is not None,
        "bad saved SHA",
    )
    require(positive(state.get("issue")) and isinstance(state.get("repository"), str), "bad saved identity")
    lease = mapping(state.get("lease"), "saved lease")
    require(isinstance(lease.get("holder"), str), "saved lease holder missing")
    require(positive(lease.get("expires_at")), "saved lease expiry missing")
    return state


def render_handoff(body: str, state: dict[str, Any]) -> str:
    block = f"{START}\n{json.dumps(state, sort_keys=True, separators=(',', ':'))}\n{END}"
    if START not in body and END not in body:
        return body.rstrip() + "\n\n" + block + "\n"
    start = body.index(START)
    end = body.index(END) + len(END)
    return body[:start] + block + body[end:]


def matching_prs(observation: dict[str, Any], branch: str, issue: int) -> list[dict[str, Any]]:
    raw = observation.get("pull_requests")
    require(isinstance(raw, list), "pull_requests must be an explicit list, not missing")
    candidates: list[dict[str, Any]] = []
    for entry in raw:
        pr = mapping(entry, "pull_request")
        head = mapping(pr.get("head"), "pull_request.head")
        base = mapping(pr.get("base"), "pull_request.base")
        if head.get("ref") != branch or base.get("ref") != "dev":
            continue
        require(positive(pr.get("number")), "pull_request number missing")
        require(pr.get("state") in ("open", "closed"), "pull_request state invalid")
        require(isinstance(pr.get("body"), str), "pull_request body missing")
        require(re.search(rf"\bRefs\s+#{issue}\b", pr["body"]) is not None, "PR lacks Issue reference")
        candidates.append(pr)
    open_prs = [pr for pr in candidates if pr["state"] == "open"]
    require(len(open_prs) <= 1, "multiple open PRs own this work branch")
    if open_prs:
        return open_prs
    merged = [pr for pr in candidates if pr["state"] == "closed" and pr.get("merged") is True]
    require(len(merged) <= 1, "multiple merged PRs own this work branch")
    return merged


def validate_observation(observation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    issue = mapping(observation.get("issue"), "issue")
    branch = mapping(observation.get("branch"), "branch")
    require(observation.get("repository") == "mlookhere/CI-Pipline", "wrong repository")
    require(positive(issue.get("number")) and issue.get("state") == "open", "Issue must be open")
    require(isinstance(issue.get("updated_at"), str) and issue["updated_at"], "Issue timestamp missing")
    require(isinstance(issue.get("body"), str), "Issue body not supplied")
    number = issue["number"]
    name = branch.get("name")
    require(
        isinstance(name, str) and re.fullmatch(rf"work/{number}-[a-z0-9][a-z0-9-]*", name) is not None,
        "work branch does not belong to Issue",
    )
    sha = branch.get("sha")
    require(isinstance(sha, str) and SHA.fullmatch(sha) is not None, "work branch SHA missing or invalid")
    return issue, branch, sha


def check_preconditions(saved: dict[str, Any] | None, request: dict[str, Any], sha: str, now: int) -> int:
    require(request.get("expected_head_sha") == sha, "stale branch head; re-read GitHub")
    revision = saved["revision"] if saved else 0
    require(request.get("expected_revision") == revision, "stale Issue handoff revision")
    actor = request.get("actor")
    require(isinstance(actor, str) and OWNER.fullmatch(actor) is not None, "invalid actor ID")
    if saved:
        lease = saved["lease"]
        require(
            lease["holder"] == actor or lease["expires_at"] <= now,
            "a different session holds a live handoff lease",
        )
    return revision


def verify_saved_identity(saved: dict[str, Any] | None, repository: str, issue: int, branch: str) -> None:
    if saved is None:
        return
    require(saved["repository"] == repository, "saved handoff belongs to another repository")
    require(saved["issue"] == issue, "saved handoff belongs to another Issue")
    require(saved.get("branch") == branch, "saved handoff belongs to another branch")


def build_snapshot(
    observed: dict[str, Any],
    issue: dict[str, Any],
    branch: dict[str, Any],
    sha: str,
    pr: dict[str, Any] | None,
    request: dict[str, Any],
    revision: int,
    now: int,
    ttl: int,
) -> dict[str, Any]:
    phase = "merged" if pr and pr.get("merged") is True else "review" if pr else "developing"
    return {
        "version": 1,
        "revision": revision + 1,
        "repository": observed["repository"],
        "issue": issue["number"],
        "branch": branch["name"],
        "head_sha": sha,
        "pr": pr["number"] if pr else None,
        "phase": phase,
        "ci": "unverified",
        "lease": {"holder": request["actor"], "expires_at": now + ttl},
    }


def unchanged(saved: dict[str, Any] | None, proposed: dict[str, Any], actor: str) -> bool:
    if saved is None or saved["lease"]["holder"] != actor:
        return False
    old = {key: value for key, value in saved.items() if key not in ("revision", "lease")}
    new = {key: value for key, value in proposed.items() if key not in ("revision", "lease")}
    return old == new


def plan_handoff(request: dict[str, Any]) -> dict[str, Any]:
    observed = mapping(request.get("observed"), "observed")
    issue, branch, sha = validate_observation(observed)
    require(request.get("expected_issue_updated_at") == issue["updated_at"], "stale Issue timestamp")
    now = request.get("now")
    ttl = request.get("lease_seconds", 1800)
    require(positive(now), "now must be a positive epoch second")
    require(positive(ttl) and ttl <= MAX_LEASE_SECONDS, "lease TTL out of range")
    saved = read_handoff(issue["body"])
    verify_saved_identity(saved, observed["repository"], issue["number"], branch["name"])
    revision = check_preconditions(saved, request, sha, now)
    prs = matching_prs(observed, branch["name"], issue["number"])
    pr = prs[0] if prs else None
    if pr:
        require(
            isinstance(pr["head"].get("sha"), str) and pr["head"]["sha"] == sha,
            "PR head does not match observed work branch",
        )
    snapshot = build_snapshot(observed, issue, branch, sha, pr, request, revision, now, ttl)
    if unchanged(saved, snapshot, request["actor"]):
        return {"action": "unchanged", "snapshot": saved, "issue_body": issue["body"]}
    return {
        "action": "update_issue",
        "snapshot": snapshot,
        "issue_body": render_handoff(issue["body"], snapshot),
        "preconditions": {
            "repository": observed["repository"],
            "issue": issue["number"],
            "issue_updated_at": issue["updated_at"],
            "head_sha": sha,
            "revision": revision,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a GitHub-backed handoff without performing writes.")
    parser.add_argument("--input", type=argparse.FileType("r", encoding="utf-8"), default=sys.stdin)
    args = parser.parse_args()
    try:
        request = json.load(args.input)
        result = plan_handoff(mapping(request, "request"))
    except (ValueError, TypeError) as exc:
        print(f"handoff rejected: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
