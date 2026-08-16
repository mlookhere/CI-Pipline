"""The PreToolUse risk-label guard sees the file a write tool is about to touch (Issue #52).

Before this fix `missing_risk_labels` built its path list only from `*** Update File:`
headers in a command string, so an ordinary Edit or Write -- the way every session edits
files -- reached the guard with an empty list and was allowed without the Issue ever being
read. Hosted CI caught the missing label later, after the edit, the commit and the push.

The same failure has one shape and several spellings, and every one of them ends the same
way: a path the guard cannot place is a path it never matches, which is a path it allows.
So the cases below are organised around that. A file in a sibling worktree of this
repository, and a Windows extended-length spelling of a file in this one, are both
repository paths and are both judged; a scratchpad file is not, is dropped, and the drop
is recorded rather than passing in silence.

They drive the decision function, `main()`, and the hook's real entry point, for the
denied and the allowed case alike, so the guard is proven to fail before it is trusted to
pass -- and each one is written so that reverting the code it covers breaks it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

# E402: the hooks directory is not an importable package, so sys.path has to be extended
# first. Suppressed for that reason alone, matching tests/test_workflow_guards.py.
import common  # noqa: E402
import pre_tool_policy  # noqa: E402

BASH = shutil.which("bash")

CFG = {
    "github": {
        "risk_paths": {
            "risk:security": ["**/auth/**", "**/*permission*", "knowledge_nexus/config.py"],
            "risk:ci": [".github/workflows/**", ".claude/**"],
        }
    }
}


def _issue(*labels: str) -> dict:
    return {"number": 52, "labels": [{"name": name} for name in labels]}


def _labelled(*labels: str):
    """A gh_issue stand-in that tolerates the keyword the caller passes it.

    `missing_risk_labels` asks for the Issue with allow_stale=False, and a fake that only
    accepts positionals would hide that by raising where the real call succeeds.
    """
    return lambda *_, **__: _issue(*labels)


@pytest.fixture(autouse=True)
def _clear_checkout_cache():
    """Each case builds its own checkouts under a fresh tmp_path; the memo must not span them."""
    pre_tool_policy.CHECKOUT_CACHE.clear()
    yield
    pre_tool_policy.CHECKOUT_CACHE.clear()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A checkout root that is not a git repository: the decision function must not need one."""
    (tmp_path / "knowledge_nexus").mkdir()
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    return tmp_path


def git_fixture(cwd: Path, *args: str) -> None:
    """git inside a fixture repository, isolated from the developer's own configuration.

    Pointing both config files at a path that does not exist keeps a local commit template,
    signing requirement or hook directory from deciding whether these tests pass.
    """
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(cwd / "absent-gitconfig"),
        "GIT_CONFIG_SYSTEM": str(cwd / "absent-gitconfig"),
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), env=environment, capture_output=True, text=True, encoding="utf-8"
    )
    assert done.returncode == 0, done.stderr


def make_repo(path: Path, branch_name: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_fixture(path, "init", "-q")
    git_fixture(path, "checkout", "-q", "-b", branch_name)
    return path


def telemetry(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted((root / ".git" / "claude" / "telemetry").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


# --- which path is judged ------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(lambda r: str(r / "knowledge_nexus" / "config.py"), id="native-absolute"),
        pytest.param(
            lambda r: str(r / "knowledge_nexus" / "config.py").replace("\\", "/"), id="forward-slash"
        ),
        pytest.param(lambda r: "knowledge_nexus/config.py", id="relative"),
    ],
)
def test_every_path_shape_normalises_to_the_repo_relative_posix_form(root, shape):
    assert pre_tool_policy.repo_relative(root, shape(root)) == "knowledge_nexus/config.py"


def test_a_file_in_a_sibling_worktree_of_this_repository_is_still_judged(tmp_path, monkeypatch):
    """Issue #52's failure, relocated: this repository is worked through linked worktrees.

    `.github/workflows/ci-pr.yml` in a sibling worktree is this repository's CI definition
    no matter which checkout the session is sitting in. Judging it only against the current
    work tree dropped it, and a dropped path was an allowed path.
    """
    main = make_repo(tmp_path / "main", "dev")
    (main / "seed.txt").write_text("seed\n", encoding="utf-8")
    git_fixture(main, "add", "seed.txt")
    git_fixture(main, "commit", "-q", "-m", "seed")
    sibling = tmp_path / "sibling"
    git_fixture(main, "worktree", "add", "-q", "-b", "work/52-sibling", str(sibling))
    workflow = sibling / ".github" / "workflows" / "ci-pr.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text("name: ci\n", encoding="utf-8")

    assert pre_tool_policy.repo_relative(main, str(workflow)) == ".github/workflows/ci-pr.yml"

    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled("risk:security"))
    reason = pre_tool_policy.missing_risk_labels(main, CFG, "Edit", {"file_path": str(workflow)}, 52)
    assert reason is not None
    assert "risk:ci" in reason


def test_a_checkout_of_a_different_repository_is_not_this_repository(tmp_path):
    """Sameness is the shared git directory, not a shared parent folder or a similar name."""
    main = make_repo(tmp_path / "main", "dev")
    other = make_repo(tmp_path / "other", "dev")
    (other / ".github" / "workflows").mkdir(parents=True)
    assert pre_tool_policy.repo_relative(main, str(other / ".github" / "workflows" / "ci-pr.yml")) is None


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        pytest.param("\\\\?\\", "F:\\repo\\ci\\run", id="extended-length"),
        pytest.param("\\\\.\\", "F:\\repo\\ci\\run", id="device"),
    ],
)
def test_the_windows_verbatim_prefixes_come_off_before_the_path_is_judged(prefix, expected):
    assert pre_tool_policy.plain_spelling(prefix + "F:\\repo\\ci\\run") == expected


def test_the_verbatim_unc_prefix_becomes_the_plain_unc_spelling():
    assert pre_tool_policy.plain_spelling("\\\\?\\UNC\\host\\share\\ci\\run") == "\\\\host\\share\\ci\\run"


@pytest.mark.skipif(os.name != "nt", reason="an extended-length path is a Windows spelling")
def test_an_extended_length_spelling_of_a_risk_path_is_denied(root, monkeypatch):
    """`\\\\?\\` survives resolve(), so the anchor never matched the root and the write was allowed."""
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled())
    verbatim = "\\\\?\\" + str(root / "knowledge_nexus" / "config.py")
    assert pre_tool_policy.repo_relative(root, verbatim) == "knowledge_nexus/config.py"
    reason = pre_tool_policy.missing_risk_labels(root, CFG, "Write", {"file_path": verbatim}, 52)
    assert reason is not None
    assert "risk:security" in reason


def test_a_write_in_no_checkout_of_this_repository_is_dropped_and_recorded(
    root, tmp_path_factory, monkeypatch
):
    """A scratchpad file is not a repository path, so it is not judged -- but the drop is visible.

    Not judging and allowing are the same outcome here, and the only thing separating a
    scratch file from a repository path the guard failed to recognise is this record.
    """

    def explode(*_, **__):
        raise AssertionError("a scratch file outside the checkout must not reach the Issue lookup")

    monkeypatch.setattr(pre_tool_policy, "gh_issue", explode)
    elsewhere = tmp_path_factory.mktemp("scratch") / "knowledge_nexus" / "config.py"
    reason = pre_tool_policy.missing_risk_labels(root, CFG, "Write", {"file_path": str(elsewhere)}, 52)
    assert reason is None
    assert pre_tool_policy.repo_relative(root, str(elsewhere)) is None

    dropped = [item for item in telemetry(root) if item["event"] == "PolicyPathDropped"]
    assert len(dropped) == 1
    assert dropped[0]["tool"] == "Write"
    assert dropped[0]["key"] == "file_path"
    assert "config.py" in dropped[0]["path"]


# --- the decision function -----------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "key"),
    [("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")],
)
def test_each_write_tool_is_read_through_the_key_it_actually_uses(root, monkeypatch, tool, key):
    """The payload key is the whole fix, so every write tool is driven through its real one."""
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled("type:bug"))
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, tool, {key: str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is not None
    assert "risk:security" in reason
    assert "#52" in reason


def test_the_same_edit_with_the_label_present_is_allowed(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled("risk:security"))
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is None


def test_a_risk_path_named_only_in_the_replacement_text_is_not_what_is_judged(root, monkeypatch):
    """The guard reads the file the tool will write, not any path that appears in the payload."""

    def explode(*_, **__):
        raise AssertionError("a risk path quoted inside the new text is not a path being written")

    monkeypatch.setattr(pre_tool_policy, "gh_issue", explode)
    reason = pre_tool_policy.missing_risk_labels(
        root,
        CFG,
        "Edit",
        {
            "file_path": str(root / "knowledge_nexus" / "pipeline.py"),
            "old_string": "x",
            "new_string": "see knowledge_nexus/config.py and .github/workflows/ci-pr.yml",
        },
        52,
    )
    assert reason is None


def test_a_write_to_a_non_risk_path_never_consults_the_issue(root, monkeypatch):
    def explode(*_, **__):
        raise AssertionError("gh_issue must not be called for a path no risk glob matches")

    monkeypatch.setattr(pre_tool_policy, "gh_issue", explode)
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Write", {"file_path": str(root / "knowledge_nexus" / "pipeline.py")}, 52
    )
    assert reason is None


def test_a_notebook_edit_under_a_risk_directory_is_denied(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled())
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "NotebookEdit", {"notebook_path": str(root / ".claude" / "hooks" / "x.ipynb")}, 52
    )
    assert reason is not None
    assert "risk:ci" in reason


def test_edited_paths_still_reads_patch_headers(root):
    """No write tool in this configuration sends `command`, so this covers only the parser.

    It is named for the function it pins rather than for the guard, so it cannot be read as
    evidence that a write tool is checked -- the tests above are the ones that show that.
    """
    command = "*** Begin Patch\n*** Update File: knowledge_nexus/config.py\n@@\n*** End Patch"
    assert pre_tool_policy.edited_paths(root, "Edit", {"command": command}) == ["knowledge_nexus/config.py"]


def test_a_bash_command_naming_a_workflow_file_is_still_recognised(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled())
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Bash", {"command": "sed -i s/a/b/ .github/workflows/ci-pr.yml"}, 52
    )
    assert reason is not None
    assert "risk:ci" in reason


def test_the_real_configuration_maps_the_incident_path_to_security(root, monkeypatch):
    """The Issue's own example: knowledge_nexus/config.py, edited directly, must need risk:security."""
    cfg = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled("risk:ci"))
    reason = pre_tool_policy.missing_risk_labels(
        root, cfg, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is not None
    assert "risk:security" in reason


# --- what the refusal says -----------------------------------------------------------


def test_the_refusal_names_the_path_and_the_glob_that_matched(root, monkeypatch):
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled())
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / ".claude" / "hooks" / "pre_tool_policy.py")}, 52
    )
    assert reason is not None
    assert ".claude/hooks/pre_tool_policy.py" in reason
    assert ".claude/**" in reason


def test_a_bash_command_refused_on_a_scraped_token_says_which_token(root, monkeypatch):
    """The path hints scrape tokens out of a command line, so a read-only grep can trip them.

    Naming the token and the glob is the difference between a fixable refusal and a
    developer re-running the same command wondering what the hook objected to.
    """
    cfg = json.loads((ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled())
    reason = pre_tool_policy.missing_risk_labels(
        root, cfg, "Bash", {"command": "grep -rn token knowledge_nexus/auth/service.py"}, 52
    )
    assert reason is not None
    assert "knowledge_nexus/auth/service.py" in reason
    assert "**/auth/**" in reason


def test_an_unreadable_issue_is_refused_for_a_reason_that_names_the_real_problem(root, monkeypatch):
    """Fail closed, but do not send the developer to add a label that is already there.

    gh_issue returns None when `gh` is missing, unauthenticated, offline or rate-limited.
    The old refusal read as "you forgot risk:security", which is the one thing it is not.
    """
    monkeypatch.setattr(pre_tool_policy, "gh_issue", lambda *_, **__: None)
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert reason is not None
    assert "could not be read" in reason
    assert "gh issue view 52" in reason
    assert "risk:security" in reason
    assert "Risk-sensitive paths require Issue label(s)" not in reason


def test_the_label_lookup_refuses_to_answer_from_an_expired_cache(root, monkeypatch):
    """An expired copy of the Issue is not an answer to a question about permission.

    cache_json falls back to the stale entry when the command fails, which is right for a
    status summary and fail-open here: labels removed from the Issue would keep satisfying
    this check for as long as the file survived. The guard asks with allow_stale=False.
    """
    asked: list[bool] = []

    def record(_root, _number, *, allow_stale=True):
        asked.append(allow_stale)
        return None

    monkeypatch.setattr(pre_tool_policy, "gh_issue", record)
    reason = pre_tool_policy.missing_risk_labels(
        root, CFG, "Edit", {"file_path": str(root / "knowledge_nexus" / "config.py")}, 52
    )
    assert asked == [False]
    assert reason is not None


def test_cache_json_serves_a_stale_entry_only_when_the_caller_allows_it(root):
    """The two contracts, at the source: a summary may be old, a permission decision may not."""
    cache = common.state_dir(root) / "cache" / f"{hashlib.sha256(b'stale-probe').hexdigest()}.json"
    cache.write_text(json.dumps({"time": 0, "value": {"labels": []}}), encoding="utf-8")
    failing = ["git", "rev-parse", "--this-flag-does-not-exist"]
    assert common.cache_json(root, "stale-probe", failing, ttl=45) == {"labels": []}
    assert common.cache_json(root, "stale-probe", failing, ttl=45, allow_stale=False) is None


# --- main(), end to end --------------------------------------------------------------


def _drive_main(root: Path, monkeypatch, tool: str, tool_input: dict, labels: tuple[str, ...]) -> list[dict]:
    """Run the hook's entry point on one write tool's event and return everything it emitted."""
    emitted: list[dict] = []
    monkeypatch.setattr(
        pre_tool_policy,
        "read_event",
        lambda: {"tool_name": tool, "tool_input": tool_input, "session_id": "s", "cwd": str(root)},
    )
    monkeypatch.setattr(pre_tool_policy, "git_root", lambda *_: root)
    monkeypatch.setattr(pre_tool_policy, "config", lambda *_: CFG)
    monkeypatch.setattr(pre_tool_policy, "current_issue", lambda *_: 52)
    monkeypatch.setattr(pre_tool_policy, "gh_issue", _labelled(*labels))
    monkeypatch.setattr(pre_tool_policy, "foreign_lease", lambda *_: None)
    monkeypatch.setattr(pre_tool_policy, "log_event", lambda *_: None)
    monkeypatch.setattr(pre_tool_policy, "emit", emitted.append)
    assert pre_tool_policy.main() == 0
    return emitted


def _denials(emitted: list[dict]) -> list[dict]:
    return [
        item["hookSpecificOutput"]
        for item in emitted
        if item.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    ]


@pytest.mark.parametrize(
    ("tool", "key"),
    [("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")],
)
def test_main_denies_a_direct_write_to_a_risk_path_without_the_label(root, monkeypatch, tool, key):
    emitted = _drive_main(root, monkeypatch, tool, {key: str(root / "knowledge_nexus" / "config.py")}, ())
    denials = _denials(emitted)
    assert len(denials) == 1
    assert "risk:security" in denials[0]["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("tool", "key"),
    [("Edit", "file_path"), ("Write", "file_path"), ("NotebookEdit", "notebook_path")],
)
def test_main_allows_the_same_write_when_the_label_is_present(root, monkeypatch, tool, key):
    emitted = _drive_main(
        root, monkeypatch, tool, {key: str(root / "knowledge_nexus" / "config.py")}, ("risk:security",)
    )
    assert _denials(emitted) == []


# --- the real entry point ------------------------------------------------------------
#
# Every case above hands the implementation the same key the implementation reads, so all
# of them would pass on a guard that looked for a payload key Claude Code never sends --
# which is Issue #52 word for word. These two run the hook the way settings.json runs it:
# a PreToolUse event on stdin, through `.claude/hooks/run`, into a checkout on disk.


def build_checkout(tmp_path: Path) -> Path:
    """A checkout carrying only what the hook reads, on a branch naming Issue 52."""
    root = tmp_path / "checkout"
    (root / ".claude").mkdir(parents=True)
    shutil.copytree(ROOT / ".claude" / "hooks", root / ".claude" / "hooks")
    (root / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "lib" / "python.sh", root / "scripts" / "lib" / "python.sh")
    shutil.copy(ROOT / ".claude-workflow.json", root / ".claude-workflow.json")
    (root / "knowledge_nexus").mkdir()
    (root / "knowledge_nexus" / "config.py").write_text("SETTING = 1\n", encoding="utf-8")
    make_repo(root, "work/52-entry-point")
    return root


def seed_issue_cache(root: Path, number: int, labels: tuple[str, ...]) -> None:
    """Answer the Issue lookup from its own cache, so the hook never reaches the network.

    The alternative is a `gh` stub on PATH, which has to be a different file on Windows
    than on Linux; this reaches the same decision through the layer the hook already has.
    """
    digest = hashlib.sha256(f"issue:{number}".encode()).hexdigest()
    cache = root / ".git" / "claude" / "cache" / f"{digest}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    value = {"number": number, "labels": [{"name": name} for name in labels]}
    cache.write_text(json.dumps({"time": time.time(), "value": value}), encoding="utf-8")


def run_entry_point(root: Path, event: dict) -> dict:
    done = subprocess.run(
        [
            str(BASH),
            str(root / ".claude" / "hooks" / "run"),
            str(root / ".claude" / "hooks" / "pre_tool_policy.py"),
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        # The interpreter probe accepts a candidate only by running it, so hand it this one
        # rather than depending on what `python` means on the machine running the tests.
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(root), "CLAUDE_CI_PYTHON": sys.executable},
        cwd=str(root),
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout) if done.stdout.strip() else {}


def edit_event(root: Path) -> dict:
    return {
        "session_id": "entry-point",
        "cwd": str(root),
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(root / "knowledge_nexus" / "config.py"),
            "old_string": "SETTING = 1",
            "new_string": "SETTING = 2",
        },
    }


@pytest.mark.skipif(BASH is None, reason="the hook entry point is bash; no bash on PATH")
def test_the_real_entry_point_denies_an_unlabelled_edit_of_a_risk_path(tmp_path):
    root = build_checkout(tmp_path)
    seed_issue_cache(root, 52, ("type:bug",))
    payload = run_entry_point(root, edit_event(root))
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "risk:security" in payload["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.skipif(BASH is None, reason="the hook entry point is bash; no bash on PATH")
def test_the_real_entry_point_allows_the_same_edit_once_the_label_is_on_the_issue(tmp_path):
    root = build_checkout(tmp_path)
    seed_issue_cache(root, 52, ("risk:security",))
    payload = run_entry_point(root, edit_event(root))
    assert payload.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
