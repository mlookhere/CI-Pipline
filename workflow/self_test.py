#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
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
# The same for a bare `python`. Wider on both sides than BARE_PYTHON3 has to be: dropping
# the digit makes `python.sh`, `python.exe` and any `.../python` path fragment collide, so
# a directory separator before, or a `.` or `-` after, disqualifies the match.
BARE_PYTHON = re.compile(r"(?<![-\w./\\])python3?(?![\w.\-])")
# Hook commands go through this wrapper rather than naming an interpreter (Issue #38).
HOOK_RUNNER = ".claude/hooks/run"
SUBPROCESS_READERS = {"run", "check_output", "Popen"}
# Redirection targets that hand output nowhere a codec could apply.
NON_CAPTURING_TARGETS = {"DEVNULL", "STDOUT"}
EXECUTABLES = (
    "flow",
    "ci/run",
    "scripts/bootstrap",
    "scripts/setup-github",
    "scripts/claude-lease",
    "scripts/claude-exec",
    "scripts/validate-workflow",
    "ops/deploy-development",
    "ops/deploy-production",
    "ops/healthcheck",
    "ops/rollback-production",
)


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
    if BARE_PYTHON.search(command):
        failures.append(
            f".claude/settings.json: {event} names an interpreter directly; launch the hook "
            f"through {HOOK_RUNNER}, which probes one interpreter, memoises it, and says so "
            "loudly when nothing resolves"
        )
    elif HOOK_RUNNER not in command:
        failures.append(
            f".claude/settings.json: {event} does not go through {HOOK_RUNNER}, so a hook that "
            "cannot start would fail silently"
        )
    timeout = int(hook.get("timeout", 0) or 0)
    if timeout <= 0 or timeout > 60:
        failures.append(f".claude/settings.json: {event} timeout must be within 1..60 seconds")
    return failures


def check_executables() -> list[str]:
    failures = []
    for relative in EXECUTABLES:
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
    failures += check_executables()
    failures += check_pr_template()
    failures += check_labels(config)
    warnings += collect_warnings(config)
    failures += check_tracked_artifacts()
    failures += check_subprocess_decoding()
    failures += check_entry_point_interpreters()

    check_python(failures)

    policy = subprocess.run(
        [sys.executable, str(ROOT / "workflow" / "check_workflow_policy.py")],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if policy.returncode != 0:
        failures.extend(line for line in policy.stdout.splitlines() if line.startswith("failure:"))

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
