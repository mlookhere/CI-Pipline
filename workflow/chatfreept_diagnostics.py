"""Deterministic, redacted CI diagnostics from authenticated GitHub observations (#100).

No network, background polling, automatic reruns, or merge decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SHA = re.compile(r"^[a-f0-9]{40}$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SECRET = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_|sk-)[a-z0-9_-]{12,}|"
    r"(?:authorization\s*:\s*(?:bearer|token)\s+)\S+|"
    r"(?:password|access_token|api_key)\s*[=:]\s*\S+"
)
TIME = re.compile(r"^\d{4}-\d\d-\d\d[T ][\d:.Z+-]+\s*")
INFRA = re.compile(r"(?i)\b(?:rate limit|HTTP 50[234]|runner lost|service unavailable|network timeout)\b")
ERROR = re.compile(r"(?i)\b(?:error|failed|failure|exception|traceback)\b")
MAX_LINES = 55
MAX_CHARS = 4096
MAX_FIX_ATTEMPTS = 3


def obj(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be an object")
    return value


def positive(value: Any) -> bool:
    return type(value) is int and value > 0


def redact(text: str) -> str:
    cleaned = ANSI.sub("", text)
    return SECRET.sub("[REDACTED]", cleaned)


def snippet(raw: str) -> str:
    lines = redact(raw[-24000:]).splitlines()[-MAX_LINES:]
    return "\n".join(lines)[-MAX_CHARS:]


def error_line(raw: str) -> str:
    lines = [TIME.sub("", line).strip() for line in snippet(raw).splitlines()]
    for line in lines:
        if ERROR.search(line):
            return line[:240]
    return next((line[:240] for line in reversed(lines) if line), "no readable error line")


def read_jobs(observed: dict[str, Any]) -> list[dict[str, Any]]:
    data = obj(observed.get("jobs"), "jobs")
    rows = data.get("jobs")
    if not isinstance(rows, list) or not positive(data.get("total_count")):
        raise ValueError("jobs missing or empty; zero jobs cannot establish any diagnostic")
    if data["total_count"] != len(rows):
        raise ValueError("jobs pagination is incomplete")
    return [obj(row, "job") for row in rows]


def check_scope(observed: dict[str, Any]) -> list[dict[str, Any]]:
    head = observed.get("head_sha")
    if not isinstance(head, str) or SHA.fullmatch(head) is None:
        raise ValueError("exact head_sha missing")
    run = obj(observed.get("run"), "run")
    if run.get("head_sha") != head or not positive(run.get("id")):
        raise ValueError("run does not belong to exact head SHA")
    if not isinstance(run.get("html_url"), str) or not run["html_url"].startswith("https://github.com/"):
        raise ValueError("run URL missing or untrusted")
    jobs = read_jobs(observed)
    for job in jobs:
        if job.get("head_sha") != head or job.get("run_id") != run["id"]:
            raise ValueError("job belongs to another run or head SHA")
        if not positive(job.get("id")) or not isinstance(job.get("name"), str):
            raise ValueError("job identifier or name missing")
    return jobs


def first_failure(jobs: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    for job in sorted(jobs, key=lambda item: item["id"]):
        if job.get("conclusion") != "failure":
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            return job, None
        failed = [obj(step, "step") for step in steps if obj(step, "step").get("conclusion") == "failure"]
        return job, min(failed, key=lambda step: step.get("number", 9999)) if failed else None
    return None


def recovery(fingerprint: str, evidence: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    previous = obj(observed.get("attempts", {}), "attempts")
    state = obj(previous.get(fingerprint, {}), "previous fingerprint state")
    count = state.get("failed_fixes", 0)
    if type(count) is not int or count < 0:
        raise ValueError("invalid previous fix counter")
    if observed.get("fix_applied") is True:
        if state.get("last_head") == evidence["head_sha"]:
            raise ValueError("fix attempt cannot be counted twice for the same head")
        count += 1
    result = {"failed_fixes": count, "last_head": evidence["head_sha"]}
    return {"state": result, "human_input_required": count >= MAX_FIX_ATTEMPTS}


def diagnose(observed: Any) -> dict[str, Any]:
    try:
        data = obj(observed, "observation")
        jobs = check_scope(data)
        run = obj(data["run"], "run")
        failure = first_failure(jobs)
        if failure is None:
            pending = run.get("status") != "completed" or any(
                job.get("status") != "completed" for job in jobs
            )
            status = "pending" if pending else "no-causal-failure"
            return {"status": status, "ci_verified": False, "head_sha": data["head_sha"]}
        job, step = failure
        logs = obj(data.get("logs"), "logs")
        raw = logs.get(str(job["id"]))
        if not isinstance(raw, str) or not raw:
            return {
                "status": "missing-logs",
                "ci_verified": False,
                "head_sha": data["head_sha"],
                "job_id": job["id"],
                "run_id": run["id"],
            }
        line = error_line(raw)
        name = step.get("name") if step else "unknown step"
        cause = f"{job['name']}\n{name}\n{line}"
        fingerprint = hashlib.sha256(cause.encode("utf-8")).hexdigest()[:20]
        attempt = recovery(fingerprint, {"head_sha": data["head_sha"]}, data)
        return {
            "status": "failed",
            "ci_verified": False,
            "classification": "possible-infrastructure" if INFRA.search(line) else "deterministic-or-unknown",
            "fingerprint": fingerprint,
            "head_sha": data["head_sha"],
            "run_id": run["id"],
            "run_url": run["html_url"],
            "job_id": job["id"],
            "job_url": job.get("html_url"),
            "step": name,
            "log_excerpt": snippet(raw),
            "attempts": attempt,
        }
    except (TypeError, ValueError) as exc:
        return {"status": "invalid-evidence", "ci_verified": False, "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline causal diagnostics from GitHub MCP observations.")
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        result = diagnose(data)
    except (OSError, UnicodeError, ValueError) as exc:
        result = {"status": "invalid-evidence", "ci_verified": False, "error": str(exc)}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] in ("pending", "no-causal-failure") else 1


if __name__ == "__main__":
    raise SystemExit(main())
