#!/usr/bin/env python3
"""Safe, explicit ownership-based synchronization of the ChatGPT CI plane.

No Claude assets, no remote fetches, no automatic overwrites of consumer edits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TREES = ("ci", "scripts", "workflow")
WORKFLOWS = frozenset(("ci-pr.yml", "ci-release.yml", "security.yml", "nightly-quality.yml"))
FILES = (".chatfreept/ci.json", ".pre-commit-config.yaml", ".gitattributes")
EXCLUDED = frozenset(
    (
        "ci/mypy-advisory.ini",
        "scripts/setup-github",
        "scripts/claude-exec",
        "scripts/claude-lease",
        "workflow/handle_pr_state.py",
        "workflow/sanitize_claude_input.py",
        "workflow/render_claude_review.py",
        "workflow/schemas/claude-review.schema.json",
    )
)
LOCK = ".chatfreept/vendor-lock.json"
INTENTIONAL = "pipeline-sync: intentional"


def allowed(name: str) -> bool:
    path = Path(name)
    if name in EXCLUDED or "claude" in name.lower() or "__pycache__" in path.parts:
        return False
    if name in FILES:
        return True
    if name.startswith(".github/workflows/"):
        return path.name in WORKFLOWS
    return bool(path.parts and path.parts[0] in TREES)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def portable_files(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for base in (*TREES, ".github/workflows"):
        folder = root / base
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            name = path.relative_to(root).as_posix()
            if allowed(name) and path.is_file() and not path.is_symlink():
                found[name] = digest(path)
    for name in FILES:
        path = root / name
        if path.is_file() and not path.is_symlink():
            found[name] = digest(path)
    return found


def intentional(path: Path) -> bool:
    try:
        return INTENTIONAL in path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def read_lock(root: Path) -> dict[str, str]:
    path = root / LOCK
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("unsupported consumer vendor lock")
    if raw.get("upstream") != "mlookhere/CI-Pipline":
        raise ValueError("consumer vendor lock belongs to a different upstream")
    files = raw.get("files")
    if not isinstance(files, dict):
        raise ValueError("vendor lock must declare files")
    for name, sha in files.items():
        if not isinstance(name, str) or not allowed(name) or not isinstance(sha, str):
            raise ValueError("vendor lock references an invalid owned file")
        if len(sha) != 64 or any(letter not in "0123456789abcdef" for letter in sha):
            raise ValueError("vendor lock includes a malformed digest")
    return files


def plan(upstream: Path, consumer: Path) -> dict[str, Any]:
    source = portable_files(upstream)
    if not source:
        raise ValueError(f"{upstream} contains no runtime-neutral CI files")
    prior = read_lock(consumer)
    changes: dict[str, str] = {}
    for name, checksum in sorted(source.items()):
        path = consumer / name
        if path.is_symlink():
            changes[name] = "conflict"
        elif not path.is_file():
            changes[name] = "missing"
        elif digest(path) == checksum:
            changes[name] = "match"
        elif intentional(path):
            changes[name] = "intentional"
        elif prior.get(name) == digest(path):
            changes[name] = "stale"
        else:
            changes[name] = "conflict"
    return {"upstream": source, "previous": prior, "changes": changes}


def merge_ignore(upstream: Path, consumer: Path, *, write: bool) -> bool:
    source = upstream / ".gitignore"
    target = consumer / ".gitignore"
    if not source.is_file():
        return False
    if target.is_symlink():
        raise ValueError("consumer .gitignore must not be a symlink")
    additions = [
        line for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    existing = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    missing = [line for line in additions if line not in existing]
    if missing and write:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            if target.stat().st_size and not target.read_bytes().endswith(b"\n"):
                stream.write("\n")
            stream.write("\n".join(missing) + "\n")
    return bool(missing)


def report(upstream: Path, consumer: Path, *, mode: str = "check") -> int:
    try:
        state = plan(upstream, consumer)
        statuses: dict[str, str] = state["changes"]
        ignore_drift = merge_ignore(upstream, consumer, write=False)
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"failure: {exc}")
        return 2
    for name, status in statuses.items():
        if status != "match":
            print(f"{status}: {name}")
    if ignore_drift:
        print("missing: .gitignore requires additive entries")
    blocked = any(value == "conflict" for value in statuses.values())
    changed = any(value in ("stale", "missing") for value in statuses.values()) or ignore_drift
    if mode != "install":
        return 1 if blocked or changed else 0
    if blocked:
        print("failure: consumer-edited owned files cannot be overwritten; mark deliberate divergence")
        return 1
    try:
        for name, status in statuses.items():
            if status not in ("missing", "stale"):
                continue
            src, target = upstream / name, consumer / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        merge_ignore(upstream, consumer, write=True)
        tracked = {
            name: sha for name, sha in state["upstream"].items()
            if statuses[name] not in ("intentional", "conflict")
        }
        lock = consumer / LOCK
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            json.dumps({"version": 1, "upstream": "mlookhere/CI-Pipline", "files": tracked}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"failure: installation aborted: {exc}")
        return 2
    print(f"installed {sum(value in ('stale', 'missing') for value in statuses.values())} owned file(s)")
    return 0


def revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or safely install owned ChatGPT CI files.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="fail on actionable owned-file drift")
    action.add_argument("--dry-run", action="store_true", help="plan install, do not write")
    action.add_argument("--install", action="store_true", help="copy missing or tracked stale owned files")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--consumer", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if not args.upstream.is_dir() or not args.consumer.is_dir():
        print("failure: upstream and consumer must be existing directories")
        return 2
    mode = "install" if args.install else "check"
    return report(args.upstream.resolve(), args.consumer.resolve(), mode=mode)


if __name__ == "__main__":
    raise SystemExit(main())
