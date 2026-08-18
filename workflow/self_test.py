#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any

from bash_tools import bash_command

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_HOOK_EVENTS = {
    "SessionStart",
    "SubagentStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "PostCompact",
    "SubagentStop",
    "Stop",
}
REQUIRED_PR_SECTIONS = {"Issue", "Result", "Implementation", "Verification", "Risk", "Remaining work"}
# `python3` as a command, not as part of resolve_python, PYTHON3_BIN, or a path fragment.
BARE_PYTHON3 = re.compile(r"(?<![-\w])python3\b")
# Any bare interpreter, not just `python3`. Wider on both sides than BARE_PYTHON3 has to
# be: dropping the digit makes `python.sh` and any `.../python` path fragment collide, so a
# directory separator before, or a `-` after, disqualifies the match. The suffixes are
# spelled out rather than allowed generally, because `python3.12` and `python.exe` are
# every bit as bare as `python` while `python.sh` is a wrapper and must not be flagged.
# `py` is the Windows launcher, which picks an interpreter this repository has not probed.
BARE_PYTHON = re.compile(r"(?<![-\w./\\])(?:python(?:3(?:\.\d+)?)?(?:\.exe)?|py)(?![\w.\-])")
# Hook commands go through this wrapper rather than naming an interpreter (Issue #38).
HOOK_RUNNER = ".claude/hooks/run"
# Where the tool taxonomy the policy hooks judge by actually lives.
HOOK_COMMON = ".claude/hooks/common.py"
# The events whose matcher decides whether a policy hook runs at all. Being outside one of
# these is not weakened enforcement, it is none: the hook is never invoked, so nothing
# fails, nothing is logged, and the tool is simply ungoverned (Issue #59).
POLICY_MATCHED_EVENTS = ("PreToolUse", "PermissionRequest", "PostToolUse", "PostToolUseFailure")
# The whole command, anchored at both ends. A substring test for the runner passes for
# `sh -c 'evil' # .claude/hooks/run` and for a runner call with `|| python x` appended;
# there is exactly one shape a hook command is allowed to take, so require it exactly.
HOOK_COMMAND = re.compile(
    r'^bash "\$CLAUDE_PROJECT_DIR/\.claude/hooks/run" '
    r'"\$CLAUDE_PROJECT_DIR/\.claude/hooks/[A-Za-z0-9_.-]+\.py"$'
)
SUBPROCESS_READERS = {"run", "check_output", "Popen"}
# Redirection targets that hand output nowhere a codec could apply.
NON_CAPTURING_TARGETS = {"DEVNULL", "STDOUT"}
# The control plane's own entry points. These ship with it and are required wherever it is
# adopted.
CONTROL_PLANE_EXECUTABLES = (
    "flow",
    "ci/run",
    "scripts/bootstrap",
    "scripts/setup-github",
    "scripts/claude-lease",
    "scripts/claude-exec",
    "scripts/validate-workflow",
)


def executables(config: dict) -> tuple[str, ...]:
    """Everything that must exist and be executable.

    The consumer's own scripts are declared in `project.executables` rather than listed here.
    The four `ops/*` deployment adapters used to be hard-coded, so a copy of this control
    plane without an `ops/` directory failed `check_executables` on the first run it ever
    did -- which is every adopter, on day one (Issue #93).
    """
    declared = config.get("project", {}).get("executables", [])
    return CONTROL_PLANE_EXECUTABLES + tuple(str(path) for path in declared)


def load_json(path: Path, failures: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(f"{path.relative_to(ROOT)}: invalid JSON: {exc}")
        return {}


def check_python(failures: list[str]) -> None:
    """Compile-check the repository's own Python.

    Scoped to tracked files: walking the working tree pulled in virtualenvs,
    build output, and vendored starter kits, which made this check dominate the
    fast gate (~90s) while reporting on code the repository does not own.
    """
    for relative in sorted(path for path in tracked_files() if path.endswith(".py")):
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (SyntaxError, UnicodeError) as exc:
            failures.append(f"{relative}: invalid Python: {exc}")


def command_exists(command: str) -> bool:
    return (
        subprocess.run(
            [bash_command(), "--noprofile", "--norc", "-c", f"command -v {command}"],
            capture_output=True,
        ).returncode
        == 0
    )


# Every prohibition `.claude/rules/command-policy.md` states in prose, as the settings entry
# that enforces it. Nothing read `permissions` before, so an edit that deleted a rule -- and
# `/permissions` deletes them one click at a time -- changed what the session was allowed to
# do and failed no gate (Issue #90). Branch protection returns 403 on this plan, which makes
# the admin-merge entry the only thing between a session and a required-checks bypass.
REQUIRED_DENY_RULES = (
    "Bash(git push --force *)",
    "Bash(git push -f *)",
    "Bash(git commit --no-verify *)",
    "Bash(git push --no-verify *)",
    "Bash(git reset --hard *)",
    "Bash(git clean -f *)",
    "Bash(gh pr merge --admin *)",
    "Bash(gh secret *)",
    "Bash(gh variable *)",
    "Bash(docker system prune *)",
)


def check_workflow_policy() -> list[str]:
    """Run the policy checker, and treat any non-zero exit as a failure.

    Failures used to be harvested only from lines starting with `failure:`, so a checker that
    *crashed* contributed none of them -- the traceback went to stderr -- and this function's
    caller returned 0. `workflow_self_test` is the first command in the fast gate, which made
    it the widest of the fail-open defects Issue #90 collected: the gate reported clean for a
    check that never ran.
    """
    policy = subprocess.run(
        [sys.executable, str(ROOT / "workflow" / "check_workflow_policy.py")],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if policy.returncode == 0:
        return []
    reported = [line for line in policy.stdout.splitlines() if line.startswith("failure:")]
    if reported:
        return reported
    detail = (policy.stderr or policy.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else "no output"
    return [
        f"check_workflow_policy.py exited {policy.returncode} without reporting a failure, "
        f"so it did not complete: {tail}"
    ]


# The directories that get copied verbatim into a repository adopting this control plane.
# `.claude-workflow.json`, `pyproject.toml`, `ci/requirements-ci.txt` and `ci/mypy-advisory.ini`
# are deliberately absent: those are the consumer's own files, and naming a package in them is
# correct rather than coupling.
# Trailing separators matter: `.claude` as a bare prefix also matches `.claude-workflow.json`,
# which is the consumer's own file and the very place these values are supposed to live.
PORTABLE_TREES = ("workflow/", "ci/", ".claude/")
PORTABLE_EXCEPTIONS = {"ci/requirements-ci.txt", "ci/mypy-advisory.ini"}


def commentary_lines(source: str) -> set[int]:
    """Lines that are comment or docstring, so prose is not read as a dependency.

    A module that *explains* why Knowledge Nexus pins chromadb is not coupled to chromadb;
    a module that assigns `WITNESS = "chromadb"` is. The distinction is the whole point of
    the check below, and without it the check is a spell-checker that fires on its own
    commit message.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not body or not isinstance(body[0], ast.Expr):
            continue
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            lines.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))

    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                lines.add(token.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return lines


def check_no_product_names(config: dict) -> list[str]:
    """No file that ships with the control plane may name the product it is shipping with.

    Fixing the seven instances Issue #93 found would leave the eighth free to appear, and it
    would appear the same way the first seven did: someone reaches for a value, the value is
    right there in the repository, and nothing objects. This objects.

    The names are taken from the configuration rather than written here, so the check works
    unchanged in whatever repository adopts the plane -- including this one, where it is the
    thing keeping the extraction honest between now and the fork.
    """
    project = config.get("project", {})
    names = {str(project.get("package_dir") or ""), str(project.get("typed_advisory_witness") or "")}
    names = {name for name in names if name}
    if not names:
        return []

    failures = []
    for relative in sorted(tracked_files()):
        if not relative.startswith(PORTABLE_TREES) or relative in PORTABLE_EXCEPTIONS:
            continue
        path = ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        skip = commentary_lines(text) if relative.endswith(".py") else set()
        for number, line in enumerate(text.splitlines(), start=1):
            if number in skip:
                continue
            for name in sorted(names):
                if name in line:
                    failures.append(
                        f"{relative}:{number}: names {name!r}, but this file is part of the "
                        "portable control plane; read it from .claude-workflow.json instead"
                    )
    return failures


def check_deny_rules(hooks_config: Any) -> list[str]:
    """The written policy and the enforced policy have to be the same policy."""
    if not isinstance(hooks_config, dict):
        return []
    permissions = hooks_config.get("permissions")
    if not isinstance(permissions, dict):
        return [".claude/settings.json: permissions block is missing"]
    deny = permissions.get("deny")
    if not isinstance(deny, list):
        return [".claude/settings.json: permissions.deny must be a list"]
    missing = [rule for rule in REQUIRED_DENY_RULES if rule not in deny]
    if not missing:
        return []
    return [
        ".claude/settings.json: permissions.deny is missing "
        f"{', '.join(repr(rule) for rule in missing)}, which .claude/rules/command-policy.md "
        "prohibits"
    ]


def check_hooks(hooks_config: Any) -> list[str]:
    """Every lifecycle event wired, pointing at a real hook, with a sane timeout."""
    failures: list[str] = []
    hooks = hooks_config.get("hooks", {}) if isinstance(hooks_config, dict) else {}
    missing_events = REQUIRED_HOOK_EVENTS - set(hooks)
    if missing_events:
        failures.append(
            f".claude/settings.json: missing lifecycle hook events: {', '.join(sorted(missing_events))}"
        )
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            failures.append(f".claude/settings.json: {event} must be a list")
            continue
        for group in groups:
            for hook in group.get("hooks", []):
                failures += check_hook_entry(event, hook)
    return failures


def hook_policy_matcher(failures: list[str]) -> str | None:
    """The matcher the hooks' own tool taxonomy implies, read from the hooks themselves.

    Loaded from source rather than restated here. Claude Code reads .claude/settings.json
    as data and cannot call into this repository's code, so the matcher in that file is a
    copy of the taxonomy by necessity -- and an uncompared copy is exactly how the
    PowerShell tool came to sit outside every matcher while the settings file two keys away
    switched it on. Comparing them is what makes common.py authoritative rather than merely
    first.
    """
    path = ROOT / HOOK_COMMON
    try:
        spec = importlib.util.spec_from_file_location("hook_common", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return str(module.POLICY_MATCHER)
    except (OSError, ImportError, AttributeError, SyntaxError) as exc:
        failures.append(f"{HOOK_COMMON}: POLICY_MATCHER could not be read ({exc})")
        return None


def check_policy_matchers(hooks_config: Any) -> list[str]:
    """Every policy hook is registered for exactly the tools its code is written to judge."""
    failures: list[str] = []
    expected = hook_policy_matcher(failures)
    if expected is None:
        return failures
    wanted = set(expected.split("|"))
    hooks = hooks_config.get("hooks", {}) if isinstance(hooks_config, dict) else {}
    for event in POLICY_MATCHED_EVENTS:
        groups = hooks.get(event) or []
        if not isinstance(groups, list):
            continue
        for group in groups:
            actual = set(str(group.get("matcher", "")).split("|"))
            if actual == wanted:
                continue
            uncovered = ", ".join(sorted(wanted - actual)) or "none"
            failures.append(
                f".claude/settings.json: {event} matcher disagrees with the tool taxonomy in "
                f"{HOOK_COMMON} (uncovered: {uncovered}). A tool outside the matcher never "
                f'reaches the hook, so no policy applies to it. Set the matcher to "{expected}", '
                "or change COMMAND_TOOLS/WRITE_TOOLS if the taxonomy is what moved."
            )
    return failures


def check_hook_entry(event: str, hook: dict) -> list[str]:
    failures = []
    command = str(hook.get("command", ""))
    # Every referenced file, not just the first: the command now names the runner and the
    # hook module, and checking only one of them leaves the other free to go missing.
    for name in re.findall(r"/\.claude/hooks/([A-Za-z0-9_.-]+)", command):
        if not (ROOT / ".claude" / "hooks" / name).is_file():
            failures.append(f".claude/settings.json: {event} references missing {name}")
    # A hook command is an entry point too, and a silently dead hook stops enforcing policy
    # without failing anything -- no gate fails, no command fails, nothing is printed, and
    # pre_tool_policy simply stops denying what it denies. On Windows both `python3` and
    # `python` are routinely the Microsoft Store stub, which is on PATH and exits without
    # running anything; on Debian and Ubuntu `python` does not exist at all (Issues #35, #38).
    if BARE_PYTHON.search(command) or BARE_PYTHON3.search(command):
        failures.append(
            f".claude/settings.json: {event} names an interpreter directly; launch the hook "
            f"through {HOOK_RUNNER}, which probes one interpreter, memoises it, and says so "
            "loudly when nothing resolves"
        )
    elif not HOOK_COMMAND.match(command):
        failures.append(
            f".claude/settings.json: {event} is not exactly a {HOOK_RUNNER} invocation, so a "
            "hook that cannot start could still fail silently; the command must be "
            f'bash "$CLAUDE_PROJECT_DIR/{HOOK_RUNNER}" followed by one quoted hook path and '
            "nothing else"
        )
    timeout = int(hook.get("timeout", 0) or 0)
    if timeout <= 0 or timeout > 60:
        failures.append(f".claude/settings.json: {event} timeout must be within 1..60 seconds")
    return failures


def check_executables(config: dict) -> list[str]:
    failures = []
    for relative in executables(config):
        path = ROOT / relative
        if not path.exists():
            failures.append(f"missing required executable: {relative}")
        elif not os.access(path, os.X_OK):
            failures.append(f"required script is not executable: {relative}")
    return failures


def check_pr_template() -> list[str]:
    pr_template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"(?m)^##\s+(.+?)\s*$", pr_template))
    missing = REQUIRED_PR_SECTIONS - headings
    return [f"PR template is missing sections: {', '.join(sorted(missing))}"] if missing else []


def check_labels(config: dict) -> list[str]:
    failures = []
    state_labels = list(config.get("github", {}).get("state_labels", []))
    if len(state_labels) != len(set(state_labels)):
        failures.append(".claude-workflow.json: duplicate state labels")
    risk_labels = set(config.get("github", {}).get("risk_paths", {}))
    if not all(label.startswith("risk:") for label in risk_labels):
        failures.append(".claude-workflow.json: every changed-path label must start with risk:")
    return failures


# Groups a stage may name while defining no commands for them, because `ci/run.py` runs them
# from code rather than from config. `quality` becomes the `__QUALITY__` sentinel at
# `ci/run.py:126-128`; an empty list there is correct rather than an oversight.
SENTINEL_GROUPS = frozenset({"quality"})


def check_stage_commands(config: dict) -> list[str]:
    """A stage may not name a command group that runs nothing.

    `ci/run.py` expands a stage into its groups and skips an empty one in silence, so a name
    written into a stage as a statement of intent and never filled in reported success
    forever. The `release` stage named eleven groups and seven were empty lists, which meant
    the required "Full regression and production build" check passed having run no
    vulnerability scan, no licence check, no clean install, no SBOM and no packaging step
    (Issue #92).

    A failure rather than a warning, deliberately: `collect_warnings` below is exactly what
    let this survive, because a warning is a thing a green gate prints. Every empty group now
    has to be either implemented or removed from the stage that claims it.
    """
    failures = []
    commands = config.get("commands", {})
    for stage, groups in sorted(config.get("stages", {}).items()):
        empty = [group for group in groups if group not in SENTINEL_GROUPS and not commands.get(group)]
        if empty:
            failures.append(
                f".claude-workflow.json: stage {stage!r} runs nothing for "
                f"{', '.join(repr(group) for group in empty)}; implement the group or remove "
                "it from the stage"
            )
    return failures


def collect_warnings(config: dict) -> list[str]:
    warnings = []
    command_groups = config.get("commands", {})
    for critical in ("unit", "build"):
        if not command_groups.get(critical):
            warnings.append(f"command group {critical!r} is empty; configure it before relying on CI")
    if tracked_migration_paths() and not command_groups.get("migration"):
        warnings.append("migration paths exist but the migration command group is empty")
    if (ROOT / "Dockerfile").exists() and not command_groups.get("image_build"):
        warnings.append("Dockerfile exists but image_build is empty")
    return warnings


def tracked_migration_paths() -> bool:
    """Only tracked paths count: vendored kits and virtualenvs are not this repo's migrations."""
    return any("migrations" in Path(path).parts for path in tracked_files())


def tracked_files() -> list[str]:
    raw = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True).stdout
    return [path for path in raw.decode("utf-8", errors="replace").split("\0") if path]


def check_tracked_artifacts() -> list[str]:
    return [
        f"generated Python artifact must not be committed: {path}"
        for path in tracked_files()
        if path.endswith(".pyc") or "__pycache__" in Path(path).parts
    ]


def subprocess_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """The names in this module that reach a subprocess reader.

    Returned as (module aliases, directly imported function names). Matching only
    `subprocess.run` would let `import subprocess as sp` or `from subprocess import run`
    reintroduce the defect in front of a green gate.
    """
    modules = {"subprocess"}
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and alias.asname:
                    modules.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SUBPROCESS_READERS:
                    functions.add(alias.asname or alias.name)
    return modules, functions


def reader_name(node: ast.Call, modules: set[str], functions: set[str]) -> str | None:
    target = node.func
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in modules
        and target.attr in SUBPROCESS_READERS
    ):
        return target.attr
    if isinstance(target, ast.Name) and target.id in functions:
        return target.id
    return None


def subprocess_reads(tree: ast.AST) -> list[tuple[int, dict[str, ast.expr | None]]]:
    """Every subprocess read in `tree`, mapped to the keyword arguments it passes.

    Values are kept, not just names: `stdout=subprocess.DEVNULL` reads nothing back while
    `stdout=subprocess.PIPE` does, and flagging the first would demand a codec for output
    nobody decodes. `**kwargs` is recorded under `"**"` because its contents cannot be known
    here, and `check_output` captures whether or not it says so.
    """
    modules, functions = subprocess_aliases(tree)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = reader_name(node, modules, functions)
        if name is None:
            continue
        keywords: dict[str, ast.expr | None] = {
            keyword.arg or "**": keyword.value for keyword in node.keywords
        }
        if name == "check_output":
            keywords["__always_captures__"] = None
        calls.append((node.lineno, keywords))
    return calls


def is_constant(node: ast.expr | None, value: object) -> bool:
    return isinstance(node, ast.Constant) and node.value is value


def attribute_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def names_codec(keywords: dict[str, ast.expr | None]) -> bool:
    """`encoding=None` names nothing: it is the locale codec spelled out."""
    return "encoding" in keywords and not is_constant(keywords["encoding"], None)


def decodes_output(keywords: dict[str, ast.expr | None]) -> bool:
    switched = any(
        name in keywords and not is_constant(keywords[name], False) for name in ("text", "universal_newlines")
    )
    return switched or names_codec(keywords)


def captures_output(keywords: dict[str, ast.expr | None]) -> bool:
    """Unrecognised redirection counts as a capture; only a known discard is exempt."""
    if "__always_captures__" in keywords or "**" in keywords:
        return True
    if "capture_output" in keywords and not is_constant(keywords["capture_output"], False):
        return True
    return any(
        attribute_name(keywords[name]) not in NON_CAPTURING_TARGETS
        for name in ("stdout", "stderr")
        if name in keywords
    )


def check_subprocess_decoding() -> list[str]:
    """A captured subprocess read must name its codec instead of inheriting the locale's.

    `text=True` with no `encoding=` decodes using the platform preferred encoding. On
    Windows that is a code page such as cp1252, while `gh` and `git` emit UTF-8, so an em
    dash in an Issue body is read as mojibake and written back corrupted -- and a byte the
    code page does not define raises inside subprocess's reader thread, leaving `stdout` as
    None with returncode 0. Both failures are invisible at the call site, which is why this
    is a gate rather than a review habit (Issue #35).
    """
    failures = []
    for relative in sorted(path for path in tracked_files() if path.endswith(".py")):
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue  # check_python reports the syntax error itself
        for line, keywords in subprocess_reads(tree):
            if decodes_output(keywords) and captures_output(keywords) and not names_codec(keywords):
                failures.append(
                    f"{relative}:{line}: captured subprocess output is decoded with the locale "
                    'codec; pass encoding="utf-8"'
                )
    return failures


def check_entry_point_interpreters() -> list[str]:
    """No entry point may depend on a bare `python3`.

    Windows CreateProcess ignores shebangs outright, and `python3` on PATH is routinely the
    Microsoft Store stub: present, executable, and exits 49 without running anything.
    scripts/lib/python.sh resolves a real interpreter by executing each candidate, so entry
    points have to go through it (Issue #35).
    """
    failures = []
    for relative in sorted(tracked_files()):
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines = text.splitlines()
        shebang = lines[0] if lines and lines[0].startswith("#!") else ""
        # Only files invoked by path are entry points. A `.py` module carries its shebang
        # vestigially -- callers run it as `"$(resolve_python)" module.py` -- so the shebang
        # is never consulted and is not a defect.
        if "python" in shebang and not relative.endswith(".py"):
            failures.append(
                f"{relative}:1: entry point uses a {shebang.strip()!r} shebang, which Windows "
                'ignores; make it a bash wrapper that execs "$(resolve_python)" instead'
            )
        # python.sh is where the probing lives, so it is the one file that may name python3.
        if not shebang.endswith(("bash", "sh")) or relative == "scripts/lib/python.sh":
            continue
        for number, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            if BARE_PYTHON3.search(line):
                failures.append(
                    f"{relative}:{number}: invokes a bare python3; source scripts/lib/python.sh "
                    "and use resolve_python (or resolve_system_python) instead"
                )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the repository-owned Claude Code workflow control plane."
    )
    parser.add_argument(
        "--ci", action="store_true", help="Treat portability warnings as CI failures where appropriate."
    )
    args = parser.parse_args()
    failures: list[str] = []
    warnings: list[str] = []

    config_path = ROOT / ".claude-workflow.json"
    config = load_json(config_path, failures)
    hooks_path = ROOT / ".claude" / "settings.json"
    hooks_config = load_json(hooks_path, failures)

    if int(config.get("version", 0)) < 2:
        failures.append(".claude-workflow.json: expected workflow schema version 2 or newer")
    for branch_kind in ("integration", "production"):
        value = str(config.get("branches", {}).get(branch_kind, ""))
        if not value:
            failures.append(f".claude-workflow.json: missing {branch_kind} branch")
        elif (
            subprocess.run(
                ["git", "check-ref-format", "--branch", value], cwd=ROOT, capture_output=True
            ).returncode
            != 0
        ):
            failures.append(f".claude-workflow.json: invalid {branch_kind} branch {value!r}")

    failures += check_hooks(hooks_config)
    failures += check_deny_rules(hooks_config)
    failures += check_policy_matchers(hooks_config)
    failures += check_executables(config)
    failures += check_pr_template()
    failures += check_labels(config)
    failures += check_stage_commands(config)
    failures += check_no_product_names(config)
    warnings += collect_warnings(config)
    failures += check_tracked_artifacts()
    failures += check_subprocess_decoding()
    failures += check_entry_point_interpreters()

    check_python(failures)

    failures += check_workflow_policy()

    if args.ci and not command_exists("git"):
        failures.append("git is required in CI")

    for warning in warnings:
        print(f"warning: {warning}")
    for failure in failures:
        print(f"failure: {failure}")
    if failures:
        print(f"Claude Code workflow self-test failed with {len(failures)} finding(s).")
        return 1
    print(f"Claude Code workflow self-test passed with {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
