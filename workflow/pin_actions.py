#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(
    r"(?P<prefix>\s*-\s+uses:\s+)(?P<repo>[^/@\s]+/[^/@\s]+)@(?P<ref>[^\s#]+)(?P<suffix>.*)$"
)
SHA = re.compile(r"[0-9a-fA-F]{40}")


def resolve(repo: str, ref: str) -> str:
    if SHA.fullmatch(ref):
        return ref.lower()
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"failed to resolve {repo}@{ref}")
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"unexpected SHA for {repo}@{ref}: {sha}")
    return sha


def check() -> int:
    failures: list[str] = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = PATTERN.match(line)
            if match and not SHA.fullmatch(match.group("ref")):
                failures.append(
                    f"{path.relative_to(ROOT)}:{lineno}: {match.group('repo')}@{match.group('ref')} is not pinned to a full commit SHA"
                )
    if failures:
        print("Unpinned Actions:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("All external GitHub Actions are pinned to full commit SHAs.")
    return 0


def pin() -> int:
    workflow_dir = ROOT / ".github" / "workflows"
    changed = 0
    for path in sorted(workflow_dir.glob("*.yml")):
        output: list[str] = []
        touched = False
        for line in path.read_text(encoding="utf-8").splitlines():
            match = PATTERN.match(line)
            if not match:
                output.append(line)
                continue
            repo, ref = match.group("repo"), match.group("ref")
            sha = resolve(repo, ref)
            suffix = match.group("suffix").rstrip()
            comment = suffix if suffix.strip().startswith("#") else f" # {ref}"
            output.append(f"{match.group('prefix')}{repo}@{sha}{comment}")
            touched = touched or sha != ref
        if touched:
            path.write_text("\n".join(output) + "\n", encoding="utf-8")
            changed += 1
            print(f"pinned: {path.relative_to(ROOT)}")
    print(f"Pinned external Actions in {changed} workflow file(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="Fail if any Action reference is not a full commit SHA"
    )
    args = parser.parse_args()
    return check() if args.check else pin()


if __name__ == "__main__":
    raise SystemExit(main())
