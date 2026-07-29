#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    cache_read = as_int(usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens"))
    cache_create = as_int(usage.get("cache_creation_input_tokens"))
    input_tokens = as_int(usage.get("input_tokens"))
    output_tokens = as_int(usage.get("output_tokens"))
    return {
        "source": str(path),
        "session_id": data.get("session_id"),
        "is_error": bool(data.get("is_error")),
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "output_tokens": output_tokens,
        "total_cost_usd": as_float(data.get("total_cost_usd")),
        "duration_ms": as_int(data.get("duration_ms")),
        "num_turns": as_int(data.get("num_turns")),
        "cache_hit_ratio": round(cache_read / max(1, input_tokens + cache_read), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Claude Code JSON usage without replaying model output."
    )
    parser.add_argument("json", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args()
    runs = [summarize(Path(value)) for value in args.json]
    aggregate: dict[str, Any] = {
        "runs": len(runs),
        "sources": [run["source"] for run in runs],
        "errors": sum(1 for run in runs if run["is_error"]),
    }
    for key in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "total_cost_usd",
        "duration_ms",
        "num_turns",
    ):
        aggregate[key] = sum((run.get(key) or 0) for run in runs)
    aggregate["cache_hit_ratio"] = round(
        aggregate["cache_read_input_tokens"]
        / max(1, aggregate["input_tokens"] + aggregate["cache_read_input_tokens"]),
        4,
    )
    text = json.dumps(aggregate, indent=2) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        try:
            output.chmod(0o600)
        except OSError:
            pass
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
