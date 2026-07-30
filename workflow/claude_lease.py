"""Inspect and release the local work lease for an Issue.

Invoked through `scripts/claude-lease`, which resolves a working interpreter first.
Living here rather than carrying its own `#!/usr/bin/env python3` shebang is the point:
that shebang is not honoured on Windows, where `python3` is a Microsoft Store stub that
exits without running anything.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def git_output(args: list[str], cwd: Path | None = None) -> str:
    """Read git output as UTF-8 rather than the locale codec.

    Repository paths are UTF-8; decoding them with a Windows code page turns a non-ASCII
    path into mojibake or raises inside subprocess's reader thread.
    """
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="replace"
    ).strip()


def root() -> Path:
    return Path(git_output(["rev-parse", "--show-toplevel"]))


def common(repository_root: Path) -> Path:
    """The shared git directory, so worktrees agree on one lease location."""
    value = Path(git_output(["rev-parse", "--git-common-dir"], cwd=repository_root))
    resolved = value if value.is_absolute() else (repository_root / value).resolve()
    return resolved / "claude"


def main() -> int:
    parser = argparse.ArgumentParser(description="Show or release a local work lease.")
    parser.add_argument("action", choices=["show", "release"])
    parser.add_argument("issue", type=int)
    args = parser.parse_args()

    path = common(root()) / "leases" / f"{args.issue}.json"
    if args.action == "show":
        print(path.read_text(encoding="utf-8") if path.exists() else f"No lease for Issue #{args.issue}")
        return 0
    if path.exists():
        path.unlink()
        print(f"Released local lease for Issue #{args.issue}")
    else:
        print(f"No lease for Issue #{args.issue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
