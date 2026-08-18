"""Tests for the fast-gate guards added by Issue #35.

The guards run against the whole repository on every fast gate, which means a guard that
quietly stopped recognising anything would report success forever. These tests pin the
recognition itself, so the gate cannot rot into a no-op.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import self_test  # noqa: E402
import validate_pr  # noqa: E402
from check_workflow_policy import job_blocks  # noqa: E402

# A job carrying this marker reports rather than gates, so it is not a required check.
ADVISORY_MARKER = "# gating: no"


def _rejects(source: str) -> bool:
    """Exactly the decision the gate makes, on exactly one call.

    Asserting against the keyword set instead would let the test pass while the gate
    disagreed -- which is how `stdout=subprocess.DEVNULL` came to be documented as accepted
    while it was in fact rejected.
    """
    calls = self_test.subprocess_reads(ast.parse(source))
    assert len(calls) == 1, f"expected exactly one subprocess call in {source!r}"
    keywords = calls[0][1]
    return (
        self_test.decodes_output(keywords)
        and self_test.captures_output(keywords)
        and not self_test.names_codec(keywords)
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('subprocess.run(["gh"], text=True, capture_output=True)', id="run-capture"),
        pytest.param('subprocess.check_output(["gh"], text=True)', id="check_output"),
        pytest.param('subprocess.Popen(["gh"], stdout=PIPE, text=True)', id="popen-pipe"),
        pytest.param(
            'subprocess.run(["gh"], universal_newlines=True, capture_output=True)', id="legacy-text"
        ),
        pytest.param('subprocess.run(["gh"], text=True, stderr=subprocess.PIPE)', id="stderr-only"),
        pytest.param(
            'subprocess.run(["gh"], text=True, capture_output=True, encoding=None)', id="encoding-none"
        ),
        pytest.param(
            'from subprocess import run\nrun(["gh"], text=True, capture_output=True)', id="from-import"
        ),
        pytest.param(
            'import subprocess as sp\nsp.run(["gh"], text=True, capture_output=True)', id="module-alias"
        ),
        pytest.param("subprocess.run(cmd, text=True, **options)", id="opaque-kwargs"),
        pytest.param('subprocess.run(["gh"], text=True, stdout=handle)', id="unknown-redirect"),
    ],
)
def test_a_decoding_capture_is_rejected(source):
    assert _rejects(source), "this shape decodes captured output with the locale codec"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'subprocess.run(["gh"], text=True, encoding="utf-8", capture_output=True)', id="encoded"
        ),
        pytest.param('subprocess.check_output(["gh"], text=True, encoding="utf-8")', id="encoded-check"),
        pytest.param('subprocess.run(["git", "ls-files", "-z"], capture_output=True)', id="byte-mode"),
        pytest.param('subprocess.run(["gh"], stdout=subprocess.DEVNULL, text=True)', id="discarded-stdout"),
        pytest.param('subprocess.run(["gh"], stderr=subprocess.DEVNULL, text=True)', id="discarded-stderr"),
        pytest.param('subprocess.run(["gh"], stderr=subprocess.STDOUT, text=True)', id="merged-stderr"),
        pytest.param('subprocess.run(["gh"], text=False, capture_output=True)', id="text-switched-off"),
        pytest.param("subprocess.run(cmd, **options)", id="opaque-without-decoding"),
    ],
)
def test_a_call_the_gate_must_leave_alone(source):
    assert not _rejects(source), "the gate must not demand a codec here"


def test_unrelated_run_calls_are_ignored():
    """`uvicorn.run` is not a subprocess read, and neither is a bare `run` with no import."""
    source = "uvicorn.run(app, text=True, capture_output=True)\nrun(['gh'], text=True)"
    assert self_test.subprocess_reads(ast.parse(source)) == []


RUNNER_COMMAND = (
    'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/run" "$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_policy.py"'
)


@pytest.mark.parametrize(
    "interpreter",
    [
        pytest.param("python3", id="store-stub-on-windows"),
        # This one used to be *asserted acceptable* here. It is the default on Debian and
        # Ubuntu without python-is-python3, where it does not exist at all (Issue #38).
        pytest.param("python", id="absent-on-debian"),
    ],
)
def test_a_hook_command_may_not_name_an_interpreter(interpreter):
    """A hook that silently fails to start stops enforcing policy without failing anything."""
    hook = {
        "command": f'{interpreter} "$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_policy.py"',
        "timeout": 10,
    }
    failures = self_test.check_hook_entry("PreToolUse", hook)
    assert any("names an interpreter directly" in failure for failure in failures)


def test_the_runner_form_is_accepted():
    assert self_test.check_hook_entry("PreToolUse", {"command": RUNNER_COMMAND, "timeout": 10}) == []


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            '"$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_policy.py"',
            id="nothing-resolves-an-interpreter",
        ),
        # A substring test for the runner passes for both of these. The runner is named,
        # and does not run.
        pytest.param(
            "sh -c 'echo skipped' # .claude/hooks/run",
            id="runner-named-only-in-a-comment",
        ),
        pytest.param(
            f"{RUNNER_COMMAND} || true",
            id="runner-called-then-chained-past",
        ),
        pytest.param(
            f'{RUNNER_COMMAND} "$CLAUDE_PROJECT_DIR/.claude/hooks/stop_gate.py"',
            id="extra-argument",
        ),
    ],
)
def test_a_hook_that_bypasses_the_runner_is_rejected(command):
    """No interpreter named, but nothing reliably resolving one -- still a silent death."""
    failures = self_test.check_hook_entry("PreToolUse", {"command": command, "timeout": 10})
    assert any("not exactly a" in failure for failure in failures), failures


def test_the_eleven_hook_timeouts_are_unchanged():
    """Issue #38 requires the same timeouts, and a timeout is easy to lose in a rewrite."""
    settings = json.loads((self_test.ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    seen = [
        (event, hook["timeout"])
        for event, groups in settings["hooks"].items()
        for group in groups
        for hook in group["hooks"]
    ]
    assert seen == [
        ("SessionStart", 20),
        ("UserPromptSubmit", 10),
        ("PreToolUse", 12),
        ("PermissionRequest", 10),
        ("PostToolUse", 15),
        ("PostToolUseFailure", 15),
        ("PreCompact", 15),
        ("PostCompact", 10),
        ("SubagentStart", 10),
        ("SubagentStop", 10),
        ("Stop", 20),
    ], seen


def test_every_referenced_hook_file_is_checked_not_just_the_first():
    """The command names two files now; checking one leaves the other free to vanish."""
    hook = {
        "command": RUNNER_COMMAND.replace("pre_tool_policy.py", "no_such_hook.py"),
        "timeout": 10,
    }
    failures = self_test.check_hook_entry("PreToolUse", hook)
    assert any("references missing no_such_hook.py" in failure for failure in failures)


def test_the_real_settings_file_launches_every_hook_through_the_runner():
    """The regression this Issue exists for, pinned against the file itself."""
    settings = json.loads((self_test.ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert len(commands) == 11, commands
    assert all(self_test.HOOK_RUNNER in command for command in commands)
    assert not any(self_test.BARE_PYTHON.search(command) for command in commands)


def test_the_real_settings_file_registers_the_policy_hooks_for_every_tool_they_judge():
    """Issue #59: a hook that is never invoked enforces nothing and reports nothing.

    The matcher in settings.json is a copy of the taxonomy in .claude/hooks/common.py --
    Claude Code reads that file as data and cannot compute one -- so this comparison is
    what keeps the copy honest.
    """
    settings = json.loads((self_test.ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert self_test.check_policy_matchers(settings) == []
    assert "PowerShell" in (self_test.hook_policy_matcher([]) or "")


def test_a_matcher_that_drops_a_tool_fails_the_gate():
    """The matcher this repository shipped until Issue #59, kept as the negative case."""
    settings = {
        "hooks": {
            event: [{"matcher": "Bash|Edit|Write|NotebookEdit|mcp__.*", "hooks": []}]
            for event in self_test.POLICY_MATCHED_EVENTS
        }
    }
    failures = self_test.check_policy_matchers(settings)
    assert len(failures) == len(self_test.POLICY_MATCHED_EVENTS), failures
    assert all("PowerShell" in failure for failure in failures)


def test_a_taxonomy_that_cannot_be_read_fails_rather_than_passing_quietly(monkeypatch):
    """A comparison that could not be made is not a comparison that succeeded."""
    monkeypatch.setattr(self_test, "HOOK_COMMON", ".claude/hooks/no_such_common.py")
    failures = self_test.check_policy_matchers({"hooks": {}})
    assert any("could not be read" in failure for failure in failures), failures


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("python -c 'print(1)'", id="bare-python-command"),
        pytest.param("python3 -c 'print(1)'", id="bare-python3-command"),
        pytest.param('VALUE="$(python -c pass)"', id="substitution"),
        pytest.param("gh issue list | python -c pass", id="pipeline"),
    ],
)
def test_a_bare_python_invocation_is_recognised(text):
    assert self_test.BARE_PYTHON.search(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("scripts/lib/python.sh", id="the-resolver-itself"),
        pytest.param(". scripts/lib/python.sh", id="sourced-resolver"),
        pytest.param('PY="$(resolve_python)"', id="resolve_python"),
        pytest.param("resolve_system_python", id="resolve_system_python"),
        pytest.param("$PYTHON_BIN", id="PYTHON_BIN"),
        pytest.param(".venv/Scripts/python.exe -m pytest", id="explicit-venv-path"),
        pytest.param("/usr/local/bin/python3 -c pass", id="absolute-path"),
        pytest.param("apt install python-is-python3", id="package-name"),
    ],
)
def test_a_bare_python_match_the_gate_must_not_claim(text):
    assert not self_test.BARE_PYTHON.search(text), text


def test_the_repository_currently_satisfies_both_guards():
    assert self_test.check_subprocess_decoding() == []
    assert self_test.check_entry_point_interpreters() == []


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("python3 -c 'print(1)'", id="command"),
        pytest.param('VALUE="$(python3 -c pass)"', id="substitution"),
        pytest.param("gh issue list | python3 -c pass", id="pipeline"),
    ],
)
def test_a_bare_python3_invocation_is_recognised(line):
    assert self_test.BARE_PYTHON3.search(line)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('. "$ROOT/scripts/lib/python.sh"', id="library-path"),
        pytest.param('PY="$(resolve_python)"', id="resolver"),
        pytest.param('SYSTEM_PYTHON="$(resolve_system_python)"', id="system-resolver"),
        pytest.param('exec "$PY" workflow/claude_lease.py "$@"', id="resolved-interpreter"),
    ],
)
def test_the_resolver_pattern_is_not_mistaken_for_a_bare_interpreter(line):
    assert not self_test.BARE_PYTHON3.search(line)


# Issue #90. Two ways the self-test reported a clean gate for a check that had not run.


def _with_checker(tmp_path, monkeypatch, source: str) -> None:
    (tmp_path / "workflow").mkdir(exist_ok=True)
    (tmp_path / "workflow" / "check_workflow_policy.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(self_test, "ROOT", tmp_path)


CRASHING_CHECKERS = [
    pytest.param("import sys\nsys.exit('boom')\n", id="message-on-stderr"),
    pytest.param("raise RuntimeError('boom')\n", id="traceback"),
    pytest.param("import sys\nsys.exit(3)\n", id="silent-non-zero"),
    pytest.param("import sys\nprint('chatter')\nsys.exit(1)\n", id="output-but-no-failure-lines"),
]


@pytest.mark.parametrize("source", CRASHING_CHECKERS)
def test_a_crashing_policy_checker_is_a_failure_whatever_reached_stdout(tmp_path, monkeypatch, source):
    """Failures were harvested only from lines starting with `failure:`.

    A checker that raised contributed none of those -- its traceback went to stderr -- so
    `failures` stayed empty and `self_test` returned 0. `workflow_self_test` is the first
    command in the fast gate, which made this the widest of the fail-open defects
    (Issue #90).

    Driven through `check_workflow_policy` against a substitute checker. The first version of
    this test asserted that a phrase appeared in `self_test.py`, which a comment would have
    satisfied -- it was vacuous, and would have passed against the unfixed code.
    """
    _with_checker(tmp_path, monkeypatch, source)

    failures = self_test.check_workflow_policy()

    assert failures, "a checker that exited non-zero was reported as no findings"
    assert "did not complete" in failures[0]


def test_a_reported_failure_is_passed_through_unchanged(tmp_path, monkeypatch):
    """The synthesised failure must not displace real findings when the checker reports some."""
    _with_checker(tmp_path, monkeypatch, "import sys\nprint('failure: a real finding')\nsys.exit(1)\n")

    assert self_test.check_workflow_policy() == ["failure: a real finding"]


def test_a_passing_policy_checker_still_reports_nothing(tmp_path, monkeypatch):
    """The other direction: exit 0 must not become a failure now that every non-zero is one."""
    _with_checker(tmp_path, monkeypatch, "")

    assert self_test.check_workflow_policy() == []


DENY_RULES_THE_WRITTEN_POLICY_REQUIRES = [
    pytest.param("Bash(gh pr merge --admin *)", id="admin-merge"),
    pytest.param("Bash(git push --force *)", id="force-push"),
    pytest.param("Bash(git reset --hard *)", id="hard-reset"),
    pytest.param("Bash(docker system prune *)", id="docker-prune"),
    pytest.param("Bash(gh secret *)", id="secret-admin"),
    pytest.param("Bash(gh variable *)", id="variable-admin"),
]


@pytest.mark.parametrize("rule", DENY_RULES_THE_WRITTEN_POLICY_REQUIRES)
def test_removing_a_required_deny_rule_fails_the_self_test(rule):
    """`/permissions` deletes these one click at a time and no gate noticed (Issue #90).

    Branch protection returns 403 on this plan, so the admin-merge entry is the only thing
    standing between a session and a required-checks bypass.
    """
    settings = json.loads((self_test.ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert rule in settings["permissions"]["deny"]

    weakened = json.loads(json.dumps(settings))
    weakened["permissions"]["deny"] = [entry for entry in weakened["permissions"]["deny"] if entry != rule]
    failures = self_test.check_deny_rules(weakened)
    assert failures, f"removing {rule} was not detected"
    assert rule in failures[0]
    assert "command-policy.md" in failures[0]


def test_the_committed_settings_satisfy_the_written_policy():
    settings = json.loads((self_test.ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert self_test.check_deny_rules(settings) == []


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="no-permissions-block"),
        pytest.param({"permissions": {}}, id="no-deny-list"),
        pytest.param({"permissions": {"deny": "everything"}}, id="deny-is-not-a-list"),
    ],
)
def test_a_malformed_permissions_block_is_not_read_as_compliant(config):
    assert self_test.check_deny_rules(config) != []


# Issue #92. `ci/run.py` skips an empty command group in silence, so a stage could name a
# check it never ran and report success. The `release` stage did that seven times.


def test_a_stage_naming_an_empty_group_fails():
    config = {"stages": {"release": ["unit", "sbom"]}, "commands": {"unit": ["pytest"], "sbom": []}}

    failures = self_test.check_stage_commands(config)

    assert failures, "a stage that runs nothing for a named group was accepted"
    assert "'release'" in failures[0]
    assert "'sbom'" in failures[0]


def test_a_stage_naming_an_undefined_group_fails():
    """A typo in a stage list is the same failure as an empty group: nothing runs."""
    config = {"stages": {"pr": ["unti"]}, "commands": {"unit": ["pytest"]}}

    assert self_test.check_stage_commands(config) != []


def test_the_quality_sentinel_is_not_an_empty_group():
    """`quality` is expanded from code at `ci/run.py:126-128`, so an empty list is correct."""
    config = {"stages": {"fast": ["quality"]}, "commands": {"quality": []}}

    assert self_test.check_stage_commands(config) == []


def test_the_sentinel_is_still_handled_by_the_runner():
    """The exemption above is only safe while `ci/run.py` really does expand it."""
    source = (self_test.ROOT / "ci" / "run.py").read_text(encoding="utf-8")

    for group in self_test.SENTINEL_GROUPS:
        assert f'group == "{group}"' in source, f"{group} is exempted but the runner does not expand it"


def test_an_unreferenced_empty_group_is_allowed():
    """Placeholders a consumer may fill are fine; claiming one in a stage is not."""
    config = {"stages": {"pr": ["unit"]}, "commands": {"unit": ["pytest"], "e2e": []}}

    assert self_test.check_stage_commands(config) == []


def test_every_shipped_stage_runs_something():
    """The regression this Issue exists for, pinned against the real configuration file."""
    config = json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))

    assert self_test.check_stage_commands(config) == []


# Issue #93. The control plane is being extracted into a standalone repository, and seven
# generic modules named this product directly. Fixing the seven would leave the eighth free
# to appear, so the invariant is a gate rather than a habit.

PROJECT = {"project": {"package_dir": "knowledge_nexus", "typed_advisory_witness": "chromadb"}}


def _portable(tmp_path, monkeypatch, relative: str, source: str) -> list[str]:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(self_test, "ROOT", tmp_path)
    monkeypatch.setattr(self_test, "tracked_files", lambda: [relative])
    return self_test.check_no_product_names(PROJECT)


COUPLED_SOURCES = [
    pytest.param('PACKAGE = ROOT / "knowledge_nexus"\n', id="the-package-constant-that-was-there"),
    pytest.param('WITNESS = "chromadb"\n', id="the-witness-constant-that-was-there"),
    pytest.param('ASSETS = ("knowledge_nexus/web/index.html",)\n', id="a-packaged-asset-path"),
    pytest.param("import knowledge_nexus\n", id="an-import-of-the-product"),
]


@pytest.mark.parametrize("source", COUPLED_SOURCES)
def test_a_portable_module_naming_the_product_fails(tmp_path, monkeypatch, source):
    """Each of these is a line that was actually in the tree before this Issue."""
    failures = _portable(tmp_path, monkeypatch, "workflow/check_dependencies.py", source)

    assert failures, f"coupling was not detected in {source!r}"
    assert "read it from .claude-workflow.json" in failures[0]


COMMENTARY_SOURCES = [
    pytest.param('"""Explains why chromadb is pinned."""\n', id="module-docstring"),
    pytest.param("# knowledge_nexus/web/index.html is the asset this refers to\n", id="comment"),
    pytest.param(
        'def f():\n    """Uses knowledge_nexus as an example."""\n    return 1\n',
        id="function-docstring",
    ),
]


@pytest.mark.parametrize("source", COMMENTARY_SOURCES)
def test_prose_naming_the_product_is_not_coupling(tmp_path, monkeypatch, source):
    """A module that explains a pin is not coupled to it; one that assigns it is.

    Without this distinction the check is a spell-checker that fires on its own rationale,
    and the first response to that is to delete the rationale.
    """
    assert _portable(tmp_path, monkeypatch, "workflow/typed_advisory.py", source) == []


CONSUMER_FILES = [
    pytest.param(".claude-workflow.json", id="the-config-itself"),
    pytest.param("ci/mypy-advisory.ini", id="mypy-configuration"),
    pytest.param("ci/requirements-ci.txt", id="ci-requirements"),
    pytest.param("pyproject.toml", id="project-metadata"),
    pytest.param("tests/test_pipeline.py", id="product-tests"),
]


@pytest.mark.parametrize("relative", CONSUMER_FILES)
def test_the_consumers_own_files_may_name_the_product(tmp_path, monkeypatch, relative):
    """`.claude-workflow.json` is where these values are supposed to live.

    It also starts with `.claude`, which a bare prefix test matched -- the check reported the
    configuration file for holding the configuration.
    """
    assert _portable(tmp_path, monkeypatch, relative, 'name = "knowledge_nexus"\n') == []


def test_the_shipped_control_plane_names_no_product():
    """The regression this Issue exists for, against the real tree."""
    config = json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))

    assert self_test.check_no_product_names(config) == []


def test_the_check_is_inert_without_a_configured_name():
    """A consumer that declares no package must not have every file reported."""
    assert self_test.check_no_product_names({"project": {}}) == []


def test_the_control_plane_executables_do_not_include_the_consumers():
    """A copy without an `ops/` directory failed `check_executables` on day one."""
    assert not any(name.startswith("ops/") for name in self_test.CONTROL_PLANE_EXECUTABLES)
    assert "ops/healthcheck" in self_test.executables(
        PROJECT | {"project": {"executables": ["ops/healthcheck"]}}
    )


def test_flow_init_has_the_file_it_reads():
    """`flow init` is the first command an adopter runs, and it raised FileNotFoundError."""
    assert (self_test.ROOT / ".gitignore.claude-ci").is_file()


def test_the_token_control_block_its_readers_expect_is_declared():
    """Four modules read `token_control` and the shipped configuration defined none of it."""
    config = json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))

    assert set(config["token_control"]) >= {
        "capture_noisy_commands",
        "session_context_max_chars",
        "large_prompt_chars",
    }


@pytest.mark.parametrize("stage", ["release", "pr", "nightly", "audit"])
def test_the_gate_stages_still_name_the_checks_that_matter(stage):
    """Removing a name is the honest fix for an empty group; removing the check is not.

    The stages here lost `build`, `clean_install`, `package_release`, `sbom` and
    `dependency_sync` because this repository has no distribution to build and building one
    is not what a vendored control plane is for. They must not lose the ones that can run.
    """
    config = json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
    required = {
        "release": {"security", "unit", "coverage"},
        "pr": {"unit", "coverage"},
        "nightly": {"security", "unit", "coverage"},
        "audit": {"security"},
    }

    assert required[stage] <= set(config["stages"][stage])


# ---------------------------------------------------------- how the gates are wired up
#
# These moved here from tests/test_dependencies.py with the extraction. They never were
# about dependency declarations -- they are about whether a job that runs is a job that
# blocks, which is the same question `check_stage_commands` above asks of a stage.


def _pr_workflow() -> str:
    return (self_test.ROOT / ".github" / "workflows" / "ci-pr.yml").read_text(encoding="utf-8")


def _config() -> dict:
    return json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))


def test_a_dependency_audit_runs_on_every_pull_request():
    """Before this, findings surfaced only at release -- weeks after the change."""
    assert "security" in _config()["stages"]["audit"]


def test_the_audit_stage_has_a_job_on_the_pull_request_workflow():
    """A stage nothing invokes is a stage that never runs."""
    workflow = _pr_workflow()
    assert "./ci/run audit" in workflow
    assert "pull_request" in workflow


def test_the_audit_blocks_rather_than_reports():
    """`continue-on-error` or a `|| true` would make this advisory, which it is not.

    The job body is located by parsing rather than by slicing between two literals:
    slicing assumed `audit` precedes `pr-tests`, and reordering them would have made the
    slice empty and this assertion vacuous.
    """
    blocks = dict(job_blocks(_pr_workflow()))
    assert "audit" in blocks, sorted(blocks)
    assert "continue-on-error" not in blocks["audit"]
    assert all("||" not in command for command in _config()["commands"]["security"])


def test_the_audit_does_not_fall_back_past_strict():
    """`pip-audit --strict || pip-audit` re-runs without --strict and masks the failure."""
    commands = _config()["commands"]["security"]
    assert commands == ["pip-audit -r ci/requirements-ci.txt --strict"], commands


def test_every_pull_request_job_is_a_required_check():
    """A job that runs but is not required is a check that reports, not one that blocks.

    Easy to get wrong in the direction that does not announce itself: adding a gating job
    and forgetting this list leaves a check whose failure stops nothing, and removing a job
    without removing its name leaves a required check that never reports, which blocks
    every merge instead. Both are silent until a pull request is already open.
    """
    required = _config()["github"]["branch_protection"]["integration"]["required_checks"]
    gating = {}
    for job, body in job_blocks(_pr_workflow()):
        named = re.search(r"(?m)^    name: (.+?)\s*$", body)
        # An advisory job reports and never fails, so requiring it would be meaningless.
        # Recognised by an explicit marker, not by its name or by what its comments
        # happen to mention: matching on 'advisory' anywhere would silently drop a
        # gating job called 'Security advisory scan' from this check.
        advisory = ADVISORY_MARKER in body
        if named and not advisory:
            gating[job] = named.group(1)
    assert gating, "no gating job names found; this test is pinned to the wrong shape"
    missing = [name for name in gating.values() if name not in required]
    assert missing == [], f"jobs that run but do not gate: {missing}"


def test_the_fast_gate_stays_hermetic():
    """The advisory config exists so this setting never has to be relaxed.

    Relaxing `no_site_packages` to "fix" a typed finding stops the fast gate being
    reproducible, and one third-party release shipping stubs for a newer Python then
    collapses the whole typecheck into a single unrelated parse error.
    """
    pyproject = (self_test.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "no_site_packages = true" in pyproject
    advisory = (self_test.ROOT / "ci" / "mypy-advisory.ini").read_text(encoding="utf-8")
    # The setting, not the word: the advisory config explains the trade-off in a comment,
    # and an assertion that cannot tell those apart is one that fails for the wrong reason.
    settings = [line for line in advisory.splitlines() if not line.lstrip().startswith("#")]
    assert not any("no_site_packages" in line for line in settings), (
        "the advisory config must see third-party types"
    )


def test_no_gating_stage_uses_the_advisory_config():
    """The advisory config must never become the one a blocking stage runs.

    Empty here rather than `{"typed-advisory"}`: this repository has no third-party runtime
    dependencies to witness, so it runs the advisory check nowhere at all. The assertion is
    kept because what it forbids is what matters -- the advisory config reaching a stage
    that gates -- and that stays forbidden whether or not one runs it at all.
    """
    config = _config()
    using = {
        stage
        for stage, groups in config["stages"].items()
        for group in groups
        if any(
            "mypy-advisory" in command or "typed_advisory" in command
            for command in config["commands"].get(group) or []
        )
    }
    assert using == set(), using


# The files each Dependabot ecosystem will actually edit. Written out rather than inferred,
# because what a `directory:` resolves to is ecosystem-specific and guessing it would make
# this test agree with a wrong answer.
ECOSYSTEM_PATHS = {
    "pip": ("ci/requirements-ci.txt",),
    "github-actions": (".github/workflows/ci-pr.yml",),
}


def _declared_ecosystems() -> dict[str, set[str]]:
    """`package-ecosystem` to the labels it declares, read from the shipped dependabot config."""
    text = (self_test.ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    starts = [match.start() for match in re.finditer(r"(?m)^\s*-\s*package-ecosystem\s*:", text)]
    found = {}
    for index, start in enumerate(starts):
        block = text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        name = re.search(r"package-ecosystem\s*:\s*[\"']?([^\"'\s#]+)", block)
        labels = set(re.findall(r"(?m)^\s*-\s*[\"']([a-z]+:[a-z-]+)[\"']\s*$", block))
        if name:
            found[name.group(1)] = labels
    return found


@pytest.mark.parametrize("ecosystem", sorted(ECOSYSTEM_PATHS))
def test_each_dependabot_ecosystem_declares_the_labels_its_own_files_require(ecosystem):
    """A bot cannot relabel its pull request after the fact, so the config is the only chance.

    `ci/requirements-ci.txt` is matched by `ci/**` as well as by its own `risk:dependencies`
    entry, and the pip ecosystem declared only the latter. Every weekly update therefore
    opened a pull request that failed `PR metadata` for a missing `risk:ci` and could never be
    made to pass -- Dependabot does not add labels later, and a human editing them is a human
    doing the bot's job every week.
    """
    config = json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))
    risk_paths = config["github"]["risk_paths"]
    required = {
        label
        for label, patterns in risk_paths.items()
        for path in ECOSYSTEM_PATHS[ecosystem]
        if validate_pr.path_requires_label(path, list(patterns))
    }
    declared = _declared_ecosystems()

    assert ecosystem in declared, sorted(declared)
    assert required <= declared[ecosystem], (
        f"{ecosystem} touches {ECOSYSTEM_PATHS[ecosystem]}, which requires "
        f"{sorted(required)}; it declares {sorted(declared[ecosystem])}"
    )


# The pull_request_target event types that cannot change what the control Issue renders, and
# that fire in bursts. `./flow pr` creates a pull request and then adds its risk labels, so
# `opened` plus two `labeled` events arrive within about three seconds; under the sync job's
# cancel-in-progress group the first two runs are cancelled, and a cancelled run is reported
# on the pull request as a failed check (Issue #24).
POINTLESS_SYNC_TRIGGERS = ("labeled", "unlabeled", "synchronize")


def test_the_control_sync_does_not_run_on_events_it_cannot_react_to():
    """A trigger that cannot change the output is a red check bought for nothing.

    `claude_flow.open_issues_and_prs` asks GitHub for a pull request's number, title,
    headRefName, baseRefName, url and statusCheckRollup. Not its labels. So labelling a pull
    request cannot alter a single cell of the rendered table, and `synchronize` only ever
    anticipates a check result that the `workflow_run` trigger reports accurately once the
    run finishes.
    """
    workflow = (self_test.ROOT / ".github" / "workflows" / "sync-control.yml").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^  pull_request_target:\n(.*?)^  \w", workflow)
    assert block, "pull_request_target trigger not found"
    declared = re.search(r"(?m)^\s*types:\s*\[(.+?)\]", block.group(1))
    assert declared, "pull_request_target declares no explicit types, so it fires on all of them"
    types = {value.strip() for value in declared.group(1).split(",")}

    offenders = sorted(types & set(POINTLESS_SYNC_TRIGGERS))
    assert offenders == [], (
        f"sync-control.yml reacts to {offenders}, which cannot change the control Issue body; "
        "each one is a cancelled run reported as a failed check"
    )


def test_the_control_renderer_still_does_not_read_pull_request_labels():
    """The other half of the reasoning above, asserted where it would actually change.

    If the renderer ever starts reading a pull request's labels, the trigger list has to grow
    back, and the test above would then be enforcing a stale argument.
    """
    source = (self_test.ROOT / "workflow" / "claude_flow.py").read_text(encoding="utf-8")
    request = re.search(r"(?s)def open_issues_and_prs.*?pr_by_issue: dict", source)
    assert request, "open_issues_and_prs not found in the expected shape"
    assert "labels" not in request.group(0).split('"pr",')[-1], (
        "the pull request query now asks for labels; sync-control.yml must react to them again"
    )


def test_the_sync_group_is_scoped_to_one_trigger():
    """A shared group let unrelated triggers cancel the run reported on a pull request.

    Only `pull_request_target` runs appear as a check on a pull request. `./flow pr` fires an
    `issues` event twice immediately after creating one -- labelling the task Issue, then
    rewriting the control Issue -- and under a single group those cancelled the run whose
    result the pull request displays. The cancellation was reported as a failed check for the
    entire life of this repository (Issue #24).
    """
    workflow = (self_test.ROOT / ".github" / "workflows" / "sync-control.yml").read_text(encoding="utf-8")
    sync = dict(job_blocks(workflow)).get("sync")
    assert sync, "the sync job was not found"
    group = re.search(r"(?m)^\s*group:\s*(.+?)\s*$", sync)
    assert group, "the sync job declares no concurrency group"
    assert "github.event_name" in group.group(1), (
        f"sync runs in group {group.group(1)!r}, shared across triggers; an issues event can "
        "cancel the run a pull request reports"
    )


def _first_statement_of_main(source: str) -> str | None:
    """The first statement in `main()`, rendered, or None when there is no `main()`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "main" and node.body:
            return ast.dump(node.body[0])
    return None


def test_every_module_that_imports_the_stream_fix_calls_it_first():
    """Importing it and calling it late is the same bug with an alibi.

    `use_utf8_streams()` has to run before anything can write, including argparse's usage
    message and the interpreter's own traceback -- both go to a stderr that is still on the
    locale codec if the call sits after `parse_args()`. On Windows that codec is cp1252, so
    the process dies while reporting rather than while working, and the traceback names an
    encoding instead of whatever it was describing (Issues #77 and #7).
    """
    offenders = []
    for relative in sorted(self_test.tracked_files()):
        # Entry points only. A test module naming the function is discussing it, not calling it.
        if not relative.endswith(".py") or not relative.startswith(("workflow/", "ci/", ".claude/")):
            continue
        source = (self_test.ROOT / relative).read_text(encoding="utf-8", errors="replace")
        if "use_utf8_streams" not in source or "def main" not in source:
            continue
        first = _first_statement_of_main(source)
        if first is None or "use_utf8_streams" not in first:
            offenders.append(relative)

    assert offenders == [], f"these call use_utf8_streams() late or not at all: {offenders}"


def test_a_repository_with_no_package_is_not_warned_about_build():
    """A warning on every green run is how a repository teaches people to skip its output.

    It also kept a `build` group alive in this repository's own configuration for no reason
    but to silence itself -- config written around a check rather than because anything wanted
    it, which is the shape the plane exists to catch (Issue #11).
    """
    assert self_test.collect_warnings({"project": {}, "commands": {"unit": ["pytest"]}}) == []


def test_a_repository_with_a_package_still_is():
    assert any(
        "'build'" in warning
        for warning in self_test.collect_warnings(
            {"project": {"package_dir": "demo"}, "commands": {"unit": ["pytest"]}}
        )
    )


def test_the_shipped_configuration_warns_about_nothing():
    """Whatever the rule is, this repository must satisfy it without a placeholder group."""
    config = json.loads((self_test.ROOT / ".claude-workflow.json").read_text(encoding="utf-8"))

    assert self_test.collect_warnings(config) == []
    assert "build" not in config["commands"], "the group only ever existed to silence the warning"


def test_setup_github_claims_protection_only_when_it_installed_it():
    """The summary asserted protected branches whatever happened, four lines after warning.

    Observed while bootstrapping this repository: `SKIP_BRANCH_PROTECTION=1` printed "branch
    protection skipped by request" and then "protected integration/production branches with
    required CI checks". Branch protection is the only control this script installs that GitHub
    enforces server-side, so a false report of it is the one that matters (Issue #8).
    """
    script = (self_test.ROOT / "scripts" / "setup-github").read_text(encoding="utf-8")
    claim = "protected integration/production branches"
    # Collapsed, because the sentence used to wrap mid-phrase across two lines of the heredoc.
    assert claim in " ".join(script.split()), "the success sentence is gone; repin this test"

    unconditional = script.split("cat <<'NEXT'", 1)[1]
    assert claim not in " ".join(unconditional.split()), (
        "the summary states protected branches unconditionally, whatever actually happened"
    )
    assert 'PROTECTION_STATUS="skipped"' in script, "a deliberate skip must be recorded"
    assert 'PROTECTION_STATUS="failed"' in script, "a refusal must be recorded"
    assert 'if [[ "$PROTECTION_STATUS" == "failed" ]]; then\n  exit 1' in script, (
        "a refused protection must fail the script, not merely warn"
    )
