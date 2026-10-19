#!/usr/bin/env python3
"""Runtime-neutral stage configuration with strict migration parity (Issue #97)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ".chatfreept/ci.json"
LEGACY_PATH = ".claude-workflow.json"
FIELDS = {"version", "commands", "stages", "quality"}
STAGES = {"fast", "pr", "release", "nightly", "audit", "bootstrap-local", "bootstrap-ci"}
GROUP_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
PLACEHOLDER = re.compile(r"^\s*(?:true|:|#.*|TODO(?:\b|:)|CHANGE_ME(?:\b|:))\s*$", re.I)  #97


def nonempty_command(command: Any) -> bool:
    return isinstance(command, str) and bool(command.strip()) and PLACEHOLDER.fullmatch(command) is None


def check_commands(value: Any) -> list[str]:
    if not isinstance(value, dict) or not value:
        return ["ci.json: commands must be a nonempty object"]
    failures: list[str] = []
    for group, commands in value.items():
        if not isinstance(group, str) or not GROUP_NAME.fullmatch(group):
            failures.append(f"ci.json: invalid command-group name {group!r}")
        if not isinstance(commands, list) or not commands or any(
            not nonempty_command(command) for command in commands
        ):
            failures.append(f"ci.json: command group {group!r} is empty or contains a placeholder")
    return failures


def check_stages(value: Any, commands: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict):
        return ["ci.json: stages must be an object"]
    failures: list[str] = []
    unknown = set(value) - STAGES
    if unknown:
        failures.append(f"ci.json: unsupported stage names: {', '.join(sorted(map(str, unknown)))}")
    for stage in sorted(STAGES):
        groups = value.get(stage)
        if not isinstance(groups, list) or not groups or any(not isinstance(g, str) for g in groups):
            failures.append(f"ci.json: stage {stage!r} must reference nonempty command groups")
            continue
        if len(groups) != len(set(groups)):
            failures.append(f"ci.json: stage {stage!r} contains duplicate groups")
        for group in groups:
            if group != "quality" and group not in commands:
                failures.append(f"ci.json: stage {stage!r} references unknown group {group!r}")
    return failures


def validate_config(config: Any, legacy: Any) -> list[str]:
    if not isinstance(config, dict) or not isinstance(legacy, dict):
        return ["ci.json and legacy config must both be JSON objects"]
    failures: list[str] = []
    unknown = set(config) - FIELDS
    if unknown:
        failures.append(f"ci.json: unsupported keys: {', '.join(sorted(map(str, unknown)))}")
    if type(config.get("version")) is not int or config["version"] != 1:
        failures.append("ci.json: unsupported schema version (expected 1)")
    commands = config.get("commands")
    stages = config.get("stages")
    failures.extend(check_commands(commands))
    failures.extend(check_stages(stages, commands if isinstance(commands, dict) else {}))
    if not isinstance(config.get("quality"), dict) or not config["quality"]:
        failures.append("ci.json: quality policy must be a nonempty object")
    for section in ("commands", "stages", "quality"):
        if config.get(section) != legacy.get(section):
            failures.append(f"ci.json: {section} differs from legacy CI during migration")
    return failures


def read_config(root: Path = ROOT) -> dict[str, Any]:
    """No fallback to legacy or empty commands: invalid configuration aborts execution."""
    try:
        config = json.loads((root / CONFIG_PATH).read_text(encoding="utf-8"))
        legacy = json.loads((root / LEGACY_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"CI stage configuration unreadable: {exc}") from exc
    failures = validate_config(config, legacy)
    if failures:
        raise ValueError("; ".join(failures))
    return config


def validate_repository(root: Path = ROOT) -> list[str]:
    try:
        read_config(root)
    except ValueError as exc:
        return [str(exc)]
    return []
