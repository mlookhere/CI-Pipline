"""The CI runner never emits passing evidence without an exact checkout revision."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("chatfreept_ci_runner", ROOT / "ci" / "run.py")
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_checkout_sha_is_pinned_to_live_git_head():
    sha = runner.checked_checkout_revision()
    assert runner.SHA.fullmatch(sha)


@pytest.mark.parametrize(
    ("code", "revision"),
    [
        (1, ""),
        (0, ""),
        (0, "unavailable"),
        (0, "a" * 39),
        (0, "a" * 41),
    ],
)
def test_missing_or_invalid_git_checkout_rejects_success(monkeypatch, code, revision):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], code, revision, ""),
    )
    with pytest.raises(SystemExit, match="revision unavailable or invalid"):
        runner.checked_checkout_revision()


def test_runner_report_requires_checkout_revision_preflight():
    source = (ROOT / "ci" / "run.py").read_text(encoding="utf-8")
    assert "checkout_sha = checked_checkout_revision()" in source
    assert '"commit": checkout_sha' in source
