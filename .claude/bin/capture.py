#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# workflow/ is not an importable package. Extending the path is preferable to a second copy
# of the resolver: a captured command must run in the same bash the gates run in, or the
# capture reports a different environment than the one under test.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workflow"))

# E402 is suppressed for that reason alone -- the import cannot resolve until sys.path is
# extended above. Same pattern as ci/run.py and tests/test_workflow_policy.py.
from bash_tools import bash_command, use_utf8_streams  # noqa: E402

SENSITIVE_ENV = re.compile(
    r"(?i)(?:key|secret|token|password|passwd|cookie|credential|auth)|^(?:AWS|AZURE|GOOGLE|GITHUB|OPENAI)_"
)
REDACTIONS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(\b(?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*)[^\s,'\";]{8,}"),
]


def redact(value: str) -> str:
    result = value
    for pattern in REDACTIONS:
        if pattern.groups:
            result = pattern.sub(lambda match: (match.group(1) or "") + "[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def safe_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not SENSITIVE_ENV.search(key)}


def prune(logs: Path, *, days: int, max_bytes: int, max_files: int) -> None:
    now = time.time()
    files = sorted(
        (path for path in logs.glob("*.log") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        if now - path.stat().st_mtime > days * 86400:
            path.unlink(missing_ok=True)
    files = sorted(
        (path for path in logs.glob("*.log") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total = 0
    for index, path in enumerate(files):
        size = path.stat().st_size
        total += size
        if index >= max_files or total > max_bytes:
            path.unlink(missing_ok=True)


def main() -> int:
    # Before anything that can write to a stream, including argparse's usage errors and any
    # traceback out of `raise SystemExit(main())`. The child is decoded generously below and
    # the log file is written as UTF-8, but stdout here is a pipe, so Python encodes it with
    # the locale codec: on Windows a cp1252 that cannot carry the byte-order mark opening
    # every GitHub Actions job log line, nor the U+FFFD left by errors="replace". The result
    # was that a command which had already succeeded reported as a traceback, because the
    # wrapper died while reporting it -- and the summary naming the log file died with it,
    # so the output was not merely truncated but unreachable (Issue #77).
    use_utf8_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--encoded", required=True)
    parser.add_argument("--head", type=int, default=60)
    parser.add_argument("--tail", type=int, default=80)
    args = parser.parse_args()
    command = base64.urlsafe_b64decode(args.encoded.encode()).decode()
    root_text = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    ).stdout.strip()
    root = Path(root_text or ".").resolve()
    common = (
        subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        ).stdout.strip()
        or ".git"
    )
    common_path = Path(common)
    common_path = (root / common_path).resolve() if not common_path.is_absolute() else common_path
    logs = common_path / "claude" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    try:
        logs.chmod(0o700)
    except OSError:
        pass

    try:
        cfg = json.loads((root / ".claude-workflow.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    token = cfg.get("token_control", {})
    prune(
        logs,
        days=int(token.get("log_retention_days", 14)),
        max_bytes=int(token.get("log_max_bytes", 200_000_000)),
        max_files=int(token.get("log_max_files", 300)),
    )

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    digest = hashlib.sha256(command.encode()).hexdigest()
    path = logs / f"{stamp}-{digest[:10]}.log"
    started = time.monotonic()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(f"$ {redact(command)}\n[command-sha256 {digest}]\n\n")
        handle.flush()
        process = subprocess.Popen(
            [bash_command(), "--noprofile", "--norc", "-o", "pipefail", "-c", command],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=safe_environment(),
        )
        assert process.stdout is not None
        for line in process.stdout:
            handle.write(redact(line))
        code = process.wait()
        elapsed = time.monotonic() - started
        handle.write(f"\n[claude-capture exit={code} elapsed={elapsed:.2f}s]\n")

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"Captured redacted command output: {path}")
    print(f"Exit code: {code}; lines: {len(lines)}; elapsed: {elapsed:.2f}s")
    if len(lines) <= args.head + args.tail:
        print("\n".join(lines))
    else:
        print("\n".join(lines[: args.head]))
        print(
            f"\n... {len(lines) - args.head - args.tail} lines omitted; inspect only the relevant section in {path} ...\n"
        )
        print("\n".join(lines[-args.tail :]))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
