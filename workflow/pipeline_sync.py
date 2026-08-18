#!/usr/bin/env python3
"""Report where a vendored copy of the control plane has drifted from upstream.

The plane is distributed by vendoring, not by installing: the Claude Code hooks have to sit
in the consumer's own `.claude/` for the runtime to load them at all, so there is no version
of this that a package manager could do instead. Vendored copies drift, quietly, and the
drift that matters is not a consumer's local edit -- it is an upstream fix the consumer never
received, which looks exactly like a repository that is up to date.

So this reports rather than applies. Knowing a hook is three commits behind is the whole
value; overwriting it is a decision with a merge in it, and the consumer is the one who knows
whether their edit was deliberate.

What is compared is the code the plane owns. Configuration and tests are excluded on purpose:
`.claude-workflow.json` is where a consumer is *supposed* to differ, and reporting that as
drift would train people to ignore the report.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories the plane owns outright, and single files alongside them. Anything matching a
# tree here is compared; anything under EXCLUDED is not, however it matched.
PORTABLE_TREES = (
    ".claude/agents",
    ".claude/bin",
    ".claude/hooks",
    ".claude/rules",
    ".claude/skills",
    ".github/workflows",
    "ci",
    "scripts",
    "workflow",
)
PORTABLE_FILES = ("flow", ".pre-commit-config.yaml", ".gitignore.claude-ci")
# Consumer-owned, so a difference here is the adoption working rather than drift.
EXCLUDED = (
    "ci/requirements-ci.txt",
    "ci/mypy-advisory.ini",
)


def digest(path: Path) -> str:
    """Content hash with line endings normalised.

    A checkout on Windows can differ from one on Linux in every byte of every text file
    without a single line having changed, and a drift report that says so is a drift report
    nobody reads twice.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def portable_files(root: Path) -> dict[str, str]:
    """Every portable file under `root`, as repo-relative path to content hash."""
    found: dict[str, str] = {}
    for tree in PORTABLE_TREES:
        base = root / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in EXCLUDED:
                found[relative] = digest(path)
    for name in PORTABLE_FILES:
        path = root / name
        if path.is_file():
            found[name] = digest(path)
    return found


def revision(root: Path) -> str:
    """The upstream commit being compared against, when there is one to name."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.decode("utf-8", errors="replace").strip() or "unknown"


def compare(upstream: dict[str, str], local: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Files that differ, files upstream has and this copy does not, and the reverse."""
    differs = sorted(name for name, value in upstream.items() if name in local and local[name] != value)
    missing = sorted(set(upstream) - set(local))
    extra = sorted(set(local) - set(upstream))
    return differs, missing, extra


def report(upstream_root: Path, local_root: Path) -> int:
    upstream = portable_files(upstream_root)
    if not upstream:
        print(f"failure: {upstream_root} contains no control-plane files to compare against")
        return 2
    local = portable_files(local_root)
    differs, missing, extra = compare(upstream, local)

    for name in missing:
        print(f"missing: {name} is in the upstream plane and not in this copy")
    for name in differs:
        print(f"drifted: {name} differs from upstream")
    for name in extra:
        print(f"extra:   {name} is in this copy and not upstream")

    total = len(differs) + len(missing) + len(extra)
    against = f"{upstream_root} @ {revision(upstream_root)}"
    if total:
        print(f"\n{total} file(s) differ from {against}.")
        return 1
    print(f"{len(upstream)} control-plane file(s) match {against}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report drift against an upstream control plane.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero if there is any (currently the only mode)",
    )
    parser.add_argument(
        "--upstream",
        type=Path,
        required=True,
        help="path to a checkout of the upstream control plane",
    )
    parser.add_argument(
        "--consumer",
        type=Path,
        default=ROOT,
        help="path to the copy being checked; defaults to this repository",
    )
    args = parser.parse_args(argv)

    if not args.upstream.is_dir():
        print(f"failure: --upstream {args.upstream} is not a directory")
        return 2
    return report(args.upstream.resolve(), args.consumer.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
