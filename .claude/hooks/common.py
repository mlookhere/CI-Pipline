#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Stripe secret": re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "GitLab token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "npm token": re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "generic bearer token": re.compile(
        r"(?i)\b(?:authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{20,}"
    ),
}


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 127, "", "")


def git_root(cwd: str | None = None) -> Path | None:
    result = run(["git", "rev-parse", "--show-toplevel"], cwd=Path(cwd) if cwd else None)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 and result.stdout.strip() else None


def git_common_dir(root: Path) -> Path:
    result = run(["git", "rev-parse", "--git-common-dir"], cwd=root)
    value = result.stdout.strip() if result.returncode == 0 else ".git"
    path = Path(value)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def state_dir(root: Path) -> Path:
    path = git_common_dir(root) / "claude"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    for name in ("cache", "logs", "checkpoints", "leases", "telemetry"):
        child = path / name
        child.mkdir(parents=True, exist_ok=True)
        try:
            child.chmod(0o700)
        except OSError:
            pass
    return path


def read_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")))


def config(root: Path) -> dict[str, Any]:
    try:
        return json.loads((root / ".claude-workflow.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def git(root: Path, *args: str, timeout: int = 8) -> str:
    result = run(["git", *args], cwd=root, timeout=timeout)
    return result.stdout.strip() if result.returncode == 0 else ""


def branch(root: Path) -> str:
    return git(root, "branch", "--show-current") or "detached"


def issue_number_from_branch(value: str) -> int | None:
    match = re.search(r"(?:^|/)(\d+)(?:-|$)", value)
    return int(match.group(1)) if match else None


def current_issue(root: Path) -> int | None:
    return issue_number_from_branch(branch(root))


def short_sha(root: Path) -> str:
    return git(root, "rev-parse", "--short=12", "HEAD") or "unknown"


def changed_files(root: Path) -> list[str]:
    names = set()
    for args in (
        ("diff", "--name-only", "--diff-filter=ACMR"),
        ("diff", "--cached", "--name-only", "--diff-filter=ACMR"),
    ):
        names.update(line for line in git(root, *args).splitlines() if line)
    return sorted(names)


def cache_json(root: Path, key: str, command: list[str], ttl: int = 60) -> Any:
    cache = state_dir(root) / "cache" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    now = time.time()
    try:
        stored = json.loads(cache.read_text(encoding="utf-8"))
        if now - float(stored.get("time", 0)) <= ttl:
            return stored.get("value")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    result = run(command, cwd=root, timeout=12)
    if result.returncode == 0:
        try:
            value = json.loads(result.stdout)
            cache.write_text(json.dumps({"time": now, "value": value}), encoding="utf-8")
            return value
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(cache.read_text(encoding="utf-8")).get("value")
    except (OSError, json.JSONDecodeError):
        return None


def gh_issue(root: Path, number: int) -> dict[str, Any] | None:
    value = cache_json(
        root,
        f"issue:{number}",
        ["gh", "issue", "view", str(number), "--json", "number,title,body,state,url,labels,updatedAt"],
        ttl=45,
    )
    return value if isinstance(value, dict) else None


def gh_pr_for_branch(root: Path, branch_name: str) -> dict[str, Any] | None:
    value = cache_json(
        root,
        f"pr:{branch_name}",
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch_name,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            "number,title,state,isDraft,url,reviewDecision,statusCheckRollup,updatedAt",
        ],
        ttl=45,
    )
    if isinstance(value, list) and value:
        return value[0] if isinstance(value[0], dict) else None
    return None


def gh_control_issue(root: Path) -> dict[str, Any] | None:
    title = config(root).get("github", {}).get("control_issue_title", "[CONTROL] Current repository state")
    value = cache_json(
        root,
        f"control:{title}",
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,body,url,updatedAt",
        ],
        ttl=90,
    )
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("title") == title:
                return item
    return None


def label_names(issue: dict[str, Any] | None) -> set[str]:
    if not issue:
        return set()
    return {str(item.get("name", "")) for item in issue.get("labels", []) if isinstance(item, dict)}


def section(body: str, heading: str, max_chars: int = 700) -> str:
    pattern = re.compile(rf"(?ims)^##+\s+{re.escape(heading)}\s*$\n(.*?)(?=^##+\s|\Z)")
    match = pattern.search(body or "")
    if not match:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", match.group(1).strip())
    return text[:max_chars].rstrip()


def acceptance(body: str, max_chars: int = 900) -> str:
    for name in ("Acceptance criteria", "Acceptance Criteria", "Objective"):
        value = section(body, name, max_chars)
        if value:
            return value
    checks = [line.strip() for line in (body or "").splitlines() if re.match(r"\s*[-*]\s+\[[ xX]\]", line)]
    return "\n".join(checks[:10])[:max_chars]


def check_summary(pr: dict[str, Any] | None) -> str:
    if not pr:
        return "none"
    rollup = pr.get("statusCheckRollup") or []
    states = {
        str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        for item in rollup
        if isinstance(item, dict)
    }
    if states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}:
        return "failing"
    if states & {"PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING", "EXPECTED"}:
        return "pending"
    if states and states <= {"SUCCESS", "SKIPPED", "NEUTRAL"}:
        return "passing"
    return "not run"


def scan_secrets(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text or "")]


def log_event(root: Path, event_name: str, payload: dict[str, Any]) -> None:
    record = {"time": int(time.time()), "event": event_name, **payload}
    path = state_dir(root) / "telemetry" / time.strftime("%Y-%m-%d.jsonl", time.gmtime())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def lease_path(root: Path, issue: int) -> Path:
    return state_dir(root) / "leases" / f"{issue}.json"


def read_lease(root: Path, issue: int) -> dict[str, Any] | None:
    try:
        value = json.loads(lease_path(root, issue).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def acquire_or_check_lease(
    root: Path, issue: int, session_id: str, ttl_seconds: int
) -> tuple[bool, dict[str, Any]]:
    now = int(time.time())
    existing = read_lease(root, issue)
    stale = not existing or now - int(existing.get("heartbeat", 0)) > ttl_seconds
    same = bool(existing and existing.get("session_id") == session_id)
    if stale or same:
        value = {
            "issue": issue,
            "session_id": session_id,
            "branch": branch(root),
            "cwd": str(root),
            "acquired": int(existing.get("acquired", now)) if same and existing else now,
            "heartbeat": now,
        }
        lease_path(root, issue).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return True, value
    return False, existing or {}


def heartbeat_lease(root: Path, issue: int | None, session_id: str) -> None:
    if issue is None:
        return
    lease = read_lease(root, issue)
    if lease and lease.get("session_id") == session_id:
        lease["heartbeat"] = int(time.time())
        lease_path(root, issue).write_text(json.dumps(lease, indent=2) + "\n", encoding="utf-8")


def foreign_lease(root: Path, issue: int | None, session_id: str, ttl_seconds: int) -> dict[str, Any] | None:
    if issue is None:
        return None
    lease = read_lease(root, issue)
    if not lease or lease.get("session_id") == session_id:
        return None
    if int(time.time()) - int(lease.get("heartbeat", 0)) > ttl_seconds:
        return None
    return lease


def mutation_prompt(prompt: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(?:implement|fix|change|modify|edit|add|remove|delete|refactor|migrate|upgrade|write|create|build|rename|move|commit|push)\b",
            prompt or "",
        )
    )


def mutation_command(command: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:\bgit\s+(?:add|commit|push|reset|checkout|switch|merge|rebase|cherry-pick)\b|\b(?:rm|mv|cp|mkdir|touch|sed\s+-i|perl\s+-pi|tee)\b|(?:^|\s)(?:>|>>)\s*\S|\b(?:npm|pnpm|yarn|cargo|go|pip|uv|poetry|bundle|dotnet|mvn|gradle)\s+(?:install|add|remove|update)\b)",
            command or "",
        )
    )


def compact(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"
