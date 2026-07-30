#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".claude-workflow.json"
ARTIFACT_DIR = ROOT / "artifacts" / "ci"


def ci_home() -> Path:
    """Root of the global CI runtime that scripts/bootstrap provisions."""
    override = os.environ.get("CLAUDE_CI_HOME")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "claude-code-ci" / "v2"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing {CONFIG_PATH}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid {CONFIG_PATH}: {exc}") from exc


def run_command(command: str, env: dict[str, str]) -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc)
    print(f"\n$ {command}", flush=True)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-o", "pipefail", "-c", command],
        cwd=ROOT,
        env=env,
        text=True,
    )
    ended = dt.datetime.now(dt.timezone.utc)
    return {
        "command": command,
        "exit_code": result.returncode,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_seconds": round((ended - started).total_seconds(), 3),
    }


def venv_bindir(venv: Path) -> Path | None:
    for name in ("Scripts", "bin") if os.name == "nt" else ("bin",):
        candidate = venv / name
        if candidate.is_dir():
            return candidate
    return None


def venv_python(venv: Path) -> Path | None:
    for relative in ("Scripts/python.exe", "bin/python"):
        candidate = venv / relative
        if candidate.exists():
            return candidate
    return None


def stage_environment(extra: list[str]) -> dict[str, str]:
    """Environment shared by every stage command.

    Gate tools (ruff, mypy, lizard, detect-secrets, pip-audit, build) live in
    the global CI venv; application dependencies live in the project venv. Both
    go on PATH, CI venv first, or gate commands silently resolve to unrelated
    system installs. Interpreters are exported rather than hardcoded as
    `python3`, which on Windows resolves to a Store stub that exits without
    running anything.
    """
    env = os.environ.copy()
    project_venv = ROOT / ".venv"
    for venv in (ci_home() / "venv", project_venv):
        bindir = venv_bindir(venv)
        if bindir:
            env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"

    project_python = venv_python(project_venv)
    if project_python:
        env["VIRTUAL_ENV"] = str(project_venv)
    env["CI_PYTHON"] = sys.executable
    env["PROJECT_PYTHON"] = str(project_python) if project_python else sys.executable
    if extra:
        env["CI_STAGE_ARGS"] = " ".join(extra)
    return env


def quality(mode: str, env: dict[str, str]) -> dict[str, Any]:
    # Must receive the same enriched environment as every other stage command:
    # quality.py shells out to lizard and detect-secrets, and with a bare
    # environment those resolve to whatever unrelated copies are on the system
    # PATH rather than the gate tools bootstrap installed.
    python = Path(sys.executable)
    quality_script = shlex.quote(str(ROOT / "ci" / "quality.py"))
    command = f"{shlex.quote(str(python))} {quality_script} --mode {shlex.quote(mode)}"
    return run_command(command, env)


def stage_commands(stage: str, config: dict[str, Any]) -> list[tuple[str, str]]:
    if stage in {"quality", "quality-staged"}:
        return []
    stage_map = config.get("stages", {})
    if stage not in stage_map:
        available = sorted(set(stage_map) | {"quality", "quality-staged", "list"})
        raise SystemExit(f"unknown stage {stage!r}; choose one of: {', '.join(available)}")
    result: list[tuple[str, str]] = []
    command_groups = config.get("commands", {})
    for group in stage_map[stage]:
        if group == "quality":
            result.append((group, "__QUALITY__"))
            continue
        for command in command_groups.get(group, []):
            if command.strip():
                result.append((group, command))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run repository-owned CI stages.")
    parser.add_argument("stage")
    parser.add_argument("extra", nargs="*")
    args = parser.parse_args()
    config = load_config()

    if args.stage == "list":
        for stage, groups in config.get("stages", {}).items():
            print(f"{stage}: {', '.join(groups)}")
        return 0

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    stage_started = dt.datetime.now(dt.timezone.utc)

    if args.stage == "quality-staged":
        records.append(quality("staged", stage_environment(args.extra)))
    elif args.stage == "quality":
        records.append(quality("changed", stage_environment(args.extra)))
    else:
        commands = stage_commands(args.stage, config)
        if not commands:
            print(f"SKIP: no commands configured for stage {args.stage!r}")
        for group, command in commands:
            # Resolved per command rather than once per stage. bootstrap_ci
            # creates .venv in its first command, so PROJECT_PYTHON is unknown
            # until that command has run; computing it once up front silently
            # fell back to the CI interpreter and installed the application
            # dependencies into the wrong virtualenv.
            env = stage_environment(args.extra)
            if command == "__QUALITY__":
                record = quality("changed", env)
                record["group"] = group
            else:
                record = run_command(command, env)
                record["group"] = group
            records.append(record)
            if record["exit_code"] != 0:
                break

    stage_ended = dt.datetime.now(dt.timezone.utc)
    success = all(record["exit_code"] == 0 for record in records)
    report = {
        "stage": args.stage,
        "success": success,
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        ).stdout.strip(),
        "started_at": stage_started.isoformat(),
        "ended_at": stage_ended.isoformat(),
        "records": records,
    }
    report_path = ARTIFACT_DIR / f"{args.stage}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nCI report: {report_path.relative_to(ROOT)}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
