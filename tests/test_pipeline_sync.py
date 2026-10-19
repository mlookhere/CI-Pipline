"""Explicit ChatGPT-only vendoring must distinguish stale files from intentional forks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))
import pipeline_sync as sync  # noqa: E402


def fixture(root: Path, *, marker: bool = False) -> Path:
    (root / "workflow").mkdir(parents=True)
    (root / "workflow" / "helper.py").write_text("run()\n", encoding="utf-8")
    (root / "ci").mkdir()
    (root / "ci" / "run.py").write_text("build()\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "bootstrap").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci-pr.yml").write_text("name: Test\n", encoding="utf-8")
    (root / ".github" / "workflows" / "claude-review.yml").write_text("name: Agent\n", encoding="utf-8")
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / ".claude" / "hooks" / "hook.py").write_text("claude()\n", encoding="utf-8")
    (root / ".gitignore").write_text("node_modules/\ncache/\n", encoding="utf-8")
    (root / "project.json").write_text('{"project": "consumer"}\n', encoding="utf-8")
    if marker:
        (root / "workflow" / "helper.py").write_text("# pipeline-sync: intentional\nown()\n", encoding="utf-8")
    return root


def test_identical_sources_check_clean(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    assert sync.report(upstream, upstream) == 0
    assert sync.portable_files(upstream)


def test_runtime_plane_does_not_include_agent_assets(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    files = sync.portable_files(upstream)
    assert all("claude" not in path.lower() for path in files)
    assert "workflow/helper.py" in files
    assert ".github/workflows/ci-pr.yml" in files
    assert ".gitignore" not in files


def test_initial_install_and_check_parity(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / ".gitignore").write_text("local-only/\n", encoding="utf-8")
    assert sync.report(upstream, consumer, mode="check") == 1
    assert sync.report(upstream, consumer, mode="install") == 0
    assert sync.report(upstream, consumer, mode="check") == 0
    assert "local-only/" in (consumer / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in (consumer / ".gitignore").read_text(encoding="utf-8")
    assert not (consumer / ".claude").exists()
    assert not (consumer / ".github/workflows/claude-review.yml").exists()
    assert (consumer / "project.json").exists() is False


@pytest.mark.parametrize("number", (1, 2))
def test_tracked_legal_upstream_update_to_two_consumers(tmp_path, number):
    upstream = fixture(tmp_path / "upstream")
    consumers = [tmp_path / "a", tmp_path / "b"]
    for item in consumers:
        item.mkdir()
        assert sync.report(upstream, item, mode="install") == 0
    (upstream / "workflow/helper.py").write_text(f"fix_{number}()\n", encoding="utf-8")
    for item in consumers:
        assert sync.report(upstream, item) == 1
        assert sync.report(upstream, item, mode="install") == 0
        assert sync.report(upstream, item) == 0


def test_deliberate_divergence_is_reported_but_not_a_failure(tmp_path, capsys):
    upstream = fixture(tmp_path / "upstream")
    consumer = fixture(tmp_path / "consumer", marker=True)
    assert sync.report(upstream, consumer) == 0
    assert "intentional: workflow/helper.py" in capsys.readouterr().out


def test_consumer_extras_do_not_poison_check(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = fixture(tmp_path / "consumer")
    (consumer / "scripts/custom-deploy").write_text("deploy()\n", encoding="utf-8")
    assert sync.report(upstream, consumer) == 0


def test_unmarked_consumer_edit_is_conflict_never_overwritten(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    assert sync.report(upstream, consumer, mode="install") == 0
    own = consumer / "workflow/helper.py"
    own.write_text("consumer_override()\n", encoding="utf-8")
    original = own.read_text(encoding="utf-8")
    (upstream / "workflow/helper.py").write_text("important_fix()\n", encoding="utf-8")
    assert sync.report(upstream, consumer, mode="install") == 1
    assert own.read_text(encoding="utf-8") == original


def test_dry_run_has_check_exit_code_and_no_writes(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    assert sync.main(["--dry-run", "--upstream", str(upstream), "--consumer", str(consumer)]) == 1
    assert sync.main(["--check", "--upstream", str(upstream), "--consumer", str(consumer)]) == 1
    assert not (consumer / sync.LOCK).exists()


def test_unexpected_upstream_is_not_empty_success(tmp_path):
    upstream = tmp_path / "empty"
    upstream.mkdir()
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    assert sync.report(upstream, consumer) == 2


def test_lock_from_another_repo_is_rejected(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    assert sync.report(upstream, consumer, mode="install") == 0
    lock = consumer / sync.LOCK
    record = json.loads(lock.read_text(encoding="utf-8"))
    record["upstream"] = "untrusted/repo"
    lock.write_text(json.dumps(record), encoding="utf-8")
    assert sync.report(upstream, consumer) == 2


def test_symlink_inside_managed_scope_cannot_be_overwritten(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "workflow").mkdir()
    (consumer / "workflow/helper.py").symlink_to(upstream / "workflow/helper.py")
    assert sync.report(upstream, consumer, mode="install") == 1


def test_line_endings_do_not_create_drift(tmp_path):
    upstream = fixture(tmp_path / "upstream")
    consumer = fixture(tmp_path / "consumer")
    (consumer / "workflow/helper.py").write_bytes(b"run()\r\n")
    assert sync.report(upstream, consumer) == 0


def test_repo_contains_tracked_neutral_assets():
    files = sync.portable_files(ROOT)
    assert "workflow/pipeline_sync.py" in files
    assert ".chatfreept/ci.json" in files
    assert all(not path.startswith(".claude/") for path in files)
