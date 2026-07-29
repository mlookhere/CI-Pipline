#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

METADATA = {"release-metadata.json", "SHA256SUMS"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def payload_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"symlinks are forbidden in release bundles: {path}")
        if path.is_file() and path.relative_to(root).as_posix() not in METADATA:
            files.append(path)
    return files


def create(root: Path, commit: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    files = payload_files(root)
    if not files:
        raise SystemExit("release bundle contains no payload files")
    artifacts = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": digest(path),
            "size": path.stat().st_size,
        }
        for path in files
    ]
    metadata = {
        "version": 1,
        "source_commit": commit,
        "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifacts": artifacts,
    }
    (root / "release-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (root / "SHA256SUMS").write_text(
        "".join(f"{item['sha256']}  {item['path']}\n" for item in artifacts),
        encoding="utf-8",
    )
    print(f"Created release manifest for {len(artifacts)} payload file(s) at commit {commit}.")
    return 0


def verify(root: Path, commit: str) -> int:
    metadata_path = root / "release-metadata.json"
    sums_path = root / "SHA256SUMS"
    if not metadata_path.is_file() or not sums_path.is_file():
        raise SystemExit("release bundle is missing release-metadata.json or SHA256SUMS")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("source_commit") != commit:
        raise SystemExit(
            f"release manifest commit {metadata.get('source_commit')!r} does not match expected {commit!r}"
        )
    expected = {item["path"]: item for item in metadata.get("artifacts", [])}
    actual_paths = {path.relative_to(root).as_posix() for path in payload_files(root)}
    if actual_paths != set(expected):
        missing = sorted(set(expected) - actual_paths)
        extra = sorted(actual_paths - set(expected))
        raise SystemExit(f"release bundle contents differ from manifest; missing={missing}, extra={extra}")
    for relative, item in expected.items():
        path = root / relative
        actual_digest = digest(path)
        if actual_digest != item.get("sha256") or path.stat().st_size != item.get("size"):
            raise SystemExit(f"release artifact integrity check failed: {relative}")
    rendered = "".join(f"{expected[name]['sha256']}  {name}\n" for name in sorted(expected))
    if sums_path.read_text(encoding="utf-8") != rendered:
        raise SystemExit("SHA256SUMS does not match release-metadata.json")
    print(f"Verified release manifest for {len(expected)} payload file(s) at commit {commit}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--root", default="artifacts/release")
        command.add_argument("--commit", required=True)
    args = parser.parse_args()
    return (
        create(Path(args.root), args.commit)
        if args.command == "create"
        else verify(Path(args.root), args.commit)
    )


if __name__ == "__main__":
    raise SystemExit(main())
