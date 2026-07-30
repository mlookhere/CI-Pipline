#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def common_state() -> Path:
    value = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=ROOT, text=True, encoding="utf-8", errors="replace"
    ).strip()
    path = Path(value)
    return ((ROOT / path).resolve() if not path.is_absolute() else path.resolve()) / "claude"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "turns",
    "tool_calls",
    "errors",
)


def scan_telemetry(state: Path, cutoff: float) -> tuple[Counter[str], Counter[str], int]:
    """Tally hook events and policy decisions from JSONL telemetry inside the window."""
    event_counts: Counter[str] = Counter()
    policy_decisions: Counter[str] = Counter()
    response_chars = 0
    for path in (state / "telemetry").glob("*.jsonl"):
        if path.stat().st_mtime < cutoff:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_name = str(item.get("event", "unknown"))
            event_counts[event_name] += 1
            if event_name == "PolicyDecision":
                policy_decisions[str(item.get("category", "unknown"))] += 1
            response_chars += int(item.get("response_chars", 0) or 0)
    return event_counts, policy_decisions, response_chars


def collect_usage(state: Path, cutoff: float) -> tuple[list[Path], Counter[str]]:
    """Sum per-run token counters across every usage record inside the window."""
    files = [path for path in (state / "runs").glob("*.usage.json") if path.stat().st_mtime >= cutoff]
    usage: Counter[str] = Counter()
    for path in files:
        item = read_json(path)
        for key in USAGE_KEYS:
            usage[key] += int(item.get(key, 0) or 0)
    return files, usage


def build_report(days: int) -> tuple[dict[str, Any], Counter[str]]:
    state = common_state()
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - days * 86400
    event_counts, policy_decisions, response_chars = scan_telemetry(state, cutoff)
    usage_files, usage = collect_usage(state, cutoff)
    ci_reports = list((ROOT / "artifacts" / "ci").glob("*.json"))
    logs = list((state / "logs").glob("*.log"))
    input_tokens = usage["input_tokens"]
    report: dict[str, Any] = {
        "window_days": days,
        "hook_events": dict(event_counts),
        "policy_decisions": dict(policy_decisions),
        "captured_log_files": len(logs),
        "captured_log_bytes": sum(path.stat().st_size for path in logs),
        "bounded_tool_response_characters": response_chars,
        "active_local_leases": len(list((state / "leases").glob("*.json"))),
        "ci_reports": len(ci_reports),
        "ci_reports_passing": sum(1 for path in ci_reports if read_json(path).get("success") is True),
        "claude_runs": len(usage_files),
        "tokens": dict(usage),
        "uncached_input_tokens": max(0, input_tokens - usage["cached_input_tokens"]),
        "cache_hit_ratio": round(usage["cached_input_tokens"] / input_tokens, 4) if input_tokens else 0.0,
    }
    return report, usage


def print_report(report: dict[str, Any], usage: Counter[str]) -> None:
    print(f"Local Claude Code workflow metrics — last {report['window_days']} day(s)")
    print(
        f"Claude Code runs: {report['claude_runs']}; turns: {usage['turns']}; tool calls: {usage['tool_calls']}; errors: {usage['errors']}"
    )
    print(
        f"Input tokens: {usage['input_tokens']}; cached: {usage['cached_input_tokens']}; cache hit: {report['cache_hit_ratio']:.1%}"
    )
    print(f"Output tokens: {usage['output_tokens']}; reasoning output: {usage['reasoning_output_tokens']}")
    print(
        f"Captured logs: {report['captured_log_files']} file(s), {report['captured_log_bytes']} byte(s); bounded tool response chars: {report['bounded_tool_response_characters']}"
    )
    print(
        f"CI reports: {report['ci_reports']} total, {report['ci_reports_passing']} passing; active local leases: {report['active_local_leases']}"
    )
    for label, counts in (
        ("Hook events", report["hook_events"]),
        ("Policy decisions", report["policy_decisions"]),
    ):
        if counts:
            print(f"{label}: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize local Claude Code workflow efficiency and evidence."
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report, usage = build_report(args.days)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print_report(report, usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
