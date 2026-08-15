"""Tests for `.claude/hooks/run`, the wrapper every lifecycle hook is launched through.

This script sits on the every-tool-call path and decides whether policy hooks run at all,
so a defect in it is indistinguishable from enforcement being switched off. It is bash,
and the rest of the suite is Python, which is exactly why it went untested: the reviews of
Issue #38 found three real defects living in it, none of which any existing test could
have caught.

Each case builds a self-contained fixture repository rather than touching the real one, so
nothing here can write to the working checkout's interpreter cache.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / ".claude" / "hooks" / "run"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="the wrapper is bash; no bash on PATH")


def build(tmp_path: Path, *, resolver: bool = True, git: str | None = "dir") -> Path:
    """A fixture checkout carrying just what the wrapper reads."""
    root = tmp_path / "checkout"
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(RUNNER, root / ".claude" / "hooks" / "run")
    if resolver:
        shutil.copy(ROOT / "scripts" / "lib" / "python.sh", root / "scripts" / "lib" / "python.sh")
    if git == "dir":
        (root / ".git").mkdir()
    elif git is not None:
        (root / ".git").write_text(git, encoding="utf-8")
    (root / "hook.py").write_text("import sys\nprint('hook ran')\nsys.exit(0)\n", encoding="utf-8")
    return root


def run(root: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    environment = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    # The probe accepts a candidate only by executing it, so hand it this very interpreter
    # rather than depending on whatever `python` means on the machine running the tests.
    environment["CLAUDE_CI_PYTHON"] = sys.executable
    environment.update(env or {})
    return subprocess.run(
        [str(BASH), str(root / ".claude" / "hooks" / "run"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=str(cwd or root),
    )


def caches(root: Path) -> list[Path]:
    return sorted(root.parent.rglob("hook-interpreter"))


def test_the_hook_runs_and_its_output_survives(tmp_path):
    done = run(build(tmp_path), "hook.py")
    assert done.returncode == 0, done.stderr
    assert "hook ran" in done.stdout


def test_stdin_reaches_the_hook(tmp_path):
    root = build(tmp_path)
    (root / "hook.py").write_text("import sys;print(sys.stdin.read().strip())", encoding="utf-8")
    done = subprocess.run(
        [str(BASH), str(root / ".claude" / "hooks" / "run"), "hook.py"],
        input='{"hook_event_name":"PreToolUse"}',
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root), "CLAUDE_CI_PYTHON": sys.executable},
        cwd=str(root),
    )
    assert '"PreToolUse"' in done.stdout, done.stderr


def test_the_hooks_exit_code_propagates(tmp_path):
    root = build(tmp_path)
    (root / "hook.py").write_text("import sys;sys.exit(3)", encoding="utf-8")
    assert run(root, "hook.py").returncode == 3


def test_the_interpreter_is_memoised(tmp_path):
    root = build(tmp_path)
    assert run(root, "hook.py").returncode == 0
    cache = root / ".git" / "claude" / "cache" / "hook-interpreter"
    assert cache.is_file()
    assert Path(cache.read_text(encoding="utf-8").strip()).is_absolute()


def test_a_cache_without_a_trailing_newline_is_still_used(tmp_path):
    """`read` reports failure at EOF *after* assigning; discarding the value re-probes."""
    root = build(tmp_path)
    assert run(root, "hook.py").returncode == 0
    cache = root / ".git" / "claude" / "cache" / "hook-interpreter"
    cache.write_text(cache.read_text(encoding="utf-8").strip(), encoding="utf-8")  # no newline
    # With the resolver removed, the run can only succeed by using the cached path.
    (root / "scripts" / "lib" / "python.sh").unlink()
    done = run(root, "hook.py")
    assert done.returncode == 0, done.stderr
    assert "hook ran" in done.stdout


def test_a_stale_cache_is_re_resolved(tmp_path):
    root = build(tmp_path)
    cache = root / ".git" / "claude" / "cache" / "hook-interpreter"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("/definitely/not/here/python\n", encoding="utf-8")
    done = run(root, "hook.py")
    assert done.returncode == 0, done.stderr
    assert Path(cache.read_text(encoding="utf-8").strip()).is_absolute()


def test_a_relative_cached_path_is_refused(tmp_path):
    """Memoising a relative path hands the next call a different binary per directory."""
    root = build(tmp_path)
    cache = root / ".git" / "claude" / "cache" / "hook-interpreter"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("./python\n", encoding="utf-8")
    done = run(root, "hook.py")
    assert done.returncode == 0, done.stderr
    assert Path(cache.read_text(encoding="utf-8").strip()).is_absolute()


def test_a_relative_gitdir_keeps_the_cache_inside_the_fixture(tmp_path):
    """A submodule's .git names its directory relatively and has no /worktrees/ to strip."""
    real = tmp_path / "super" / "modules" / "sub"
    real.mkdir(parents=True)
    root = build(tmp_path, git=f"gitdir: {os.path.relpath(real, tmp_path / 'checkout')}\n")
    # Run from somewhere else entirely: a relative gitdir must not resolve against the cwd.
    done = run(root, str(root / "hook.py"), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert caches(root) == [real / "claude" / "cache" / "hook-interpreter"]


def test_a_worktree_shares_the_main_checkouts_cache(tmp_path):
    main = tmp_path / "main.git"
    (main / "worktrees" / "wt").mkdir(parents=True)
    # Written with forward slashes because that is what git writes into a .git file, on
    # Windows too. A backslash fixture here would be testing something git never produces.
    root = build(tmp_path, git=f"gitdir: {(main / 'worktrees' / 'wt').as_posix()}\n")
    done = run(root, "hook.py")
    assert done.returncode == 0, done.stderr
    assert caches(root) == [main / "claude" / "cache" / "hook-interpreter"]


def test_an_unparseable_git_file_writes_no_cache_at_all(tmp_path):
    """An empty GIT_DIR would put the cache at /claude/cache -- outside the repository.

    The fallback is `$ROOT/.git`, which in this state is the unparseable file itself, so
    creating the cache directory fails and nothing is written. That costs a probe per call
    in a repository that is already broken, and it is the safe direction to fail: the hook
    still runs, and nothing is written anywhere a different principal might own.
    """
    root = build(tmp_path, git="not a gitdir pointer\n")
    done = run(root, "hook.py")
    assert done.returncode == 0, done.stderr
    assert "hook ran" in done.stdout
    assert caches(root) == []


def test_an_unset_project_dir_is_reported(tmp_path):
    root = build(tmp_path)
    done = run(root, "hook.py", env={"CLAUDE_PROJECT_DIR": ""})
    assert done.returncode != 0
    assert "NOT enforcing" in done.stderr


def test_a_missing_resolver_is_reported_rather_than_aborting_bare(tmp_path):
    """`set -e` on a missing source aborts with bash's own message and nothing else."""
    root = build(tmp_path, resolver=False)
    done = run(root, "hook.py", env={"CLAUDE_CI_PYTHON": ""})
    assert done.returncode != 0
    assert "python.sh is missing" in done.stderr
    assert "NOT enforcing" in done.stderr


def test_an_unresolvable_interpreter_is_reported_and_does_not_poison_the_cache(tmp_path):
    root = build(tmp_path)
    done = run(
        root,
        "hook.py",
        env={
            "PATH": str(tmp_path),
            "CLAUDE_CI_PYTHON": "",
            "PYTHON_BIN": "",
            "CLAUDE_CI_HOME": str(tmp_path / "nope"),
        },
    )
    assert done.returncode != 0
    assert "NOT enforcing" in done.stderr
    assert caches(root) == []


@pytest.mark.parametrize(
    ("hook", "expected"),
    [
        # Blocking is the safe answer only where a hook exists to deny something.
        pytest.param("pre_tool_policy.py", 2, id="pre-tool-use-fails-closed"),
        pytest.param("permission_request.py", 2, id="permission-request-fails-closed"),
        # Exit 2 on Stop blocks stopping, which loops instead of reporting, and on
        # UserPromptSubmit it discards the prompt. Both report instead.
        pytest.param("stop_gate.py", 1, id="stop-must-not-block"),
        pytest.param("subagent_stop.py", 1, id="subagent-stop-must-not-block"),
        pytest.param("user_prompt_submit.py", 1, id="prompt-must-not-be-discarded"),
        pytest.param("session_context.py", 1, id="session-start"),
    ],
)
def test_the_failure_exit_code_suits_the_event(tmp_path, hook, expected):
    root = build(tmp_path, resolver=False)
    done = run(root, f"/some/where/.claude/hooks/{hook}", env={"CLAUDE_CI_PYTHON": ""})
    assert done.returncode == expected, done.stderr
    assert "NOT enforcing" in done.stderr
