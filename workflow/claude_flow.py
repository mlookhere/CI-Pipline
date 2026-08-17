#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from bash_tools import bash_command, use_utf8_streams

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".claude-workflow.json"
STATE_START = "<!-- claude-state:start -->"
STATE_END = "<!-- claude-state:end -->"
CONTROL_START = "<!-- claude-control:start -->"
CONTROL_END = "<!-- claude-control:end -->"
# What errors="replace" leaves behind when a byte could not be decoded.
REPLACEMENT = "�"
REQUIRED_STATE_HEADINGS = [
    "## Current implementation state",
    "## Decisions",
    "## Changed areas",
    "## Verification",
    "## Known problems",
    "## Next exact actions",
    "## Working references",
]


def shell(
    command: list[str], *, cwd: Path = ROOT, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    if not capture:
        print("$ " + " ".join(shlex.quote(part) for part in command), flush=True)
    # UTF-8 explicitly. `text=True` on its own decodes with the platform preferred
    # encoding, which on Windows is a code page such as cp1252, while GitHub Issue and PR
    # bodies are UTF-8. Read that way an em dash becomes three mojibake characters, and
    # cmd_handoff writes the mangled text straight back, so the corruption persists.
    # errors="replace" keeps an undecodable byte from raising inside subprocess's reader
    # thread, which would otherwise return stdout as None alongside returncode 0.
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )


def output(command: list[str], *, cwd: Path = ROOT) -> str:
    result = shell(command, cwd=cwd, check=False, capture=True)
    if result.returncode != 0:
        return ""
    if result.stdout is None:
        # "Read nothing" and "could not read" must not be indistinguishable. Callers read
        # "" as a definite absence -- no such branch, no open PR, no lease -- and act on
        # it, so a failure to capture has to stop the command instead of impersonating one.
        fail("no output could be captured from: " + " ".join(shlex.quote(part) for part in command))
    return result.stdout.strip()


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing {CONFIG_PATH}; run ./flow init")
    except json.JSONDecodeError as exc:
        fail(f"invalid {CONFIG_PATH}: {exc}")


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def gh_ready() -> bool:
    return (
        subprocess.run(
            ["gh", "auth", "status"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        == 0
    )


def issue_number_from_branch(branch: str) -> int | None:
    match = re.search(r"(?:^|/)(\d+)(?:-|$)", branch)
    return int(match.group(1)) if match else None


def release_local_lease(issue: int) -> None:
    common = output(["git", "rev-parse", "--git-common-dir"]) or ".git"
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (ROOT / common_path).resolve()
    path = common_path / "claude" / "leases" / f"{issue}.json"
    path.unlink(missing_ok=True)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


COMMAND_GROUPS = (
    "bootstrap_local",
    "bootstrap_ci",
    "format_check",
    "lint",
    "typecheck",
    "unit",
    "integration",
    "build",
    "security",
    "migration",
    "e2e",
    "image_build",
    "generated_check",
    "dependency_sync",
    "typed_advisory",
    "coverage",
    "clean_install",
    "vulnerability",
    "license",
    "sbom",
    "accessibility",
    "performance_smoke",
    "dead_code",
    "package_release",
)

NODE_SCRIPT_GROUPS = {
    "format:check": "format_check",
    "format-check": "format_check",
    "lint": "lint",
    "typecheck": "typecheck",
    "type-check": "typecheck",
    "test": "unit",
    "test:unit": "unit",
    "test:integration": "integration",
    "build": "build",
    "test:e2e": "e2e",
    "e2e": "e2e",
    "security": "security",
    "test:coverage": "coverage",
    "coverage": "coverage",
    "check:generated": "generated_check",
    "generated:check": "generated_check",
    "test:a11y": "accessibility",
    "test:accessibility": "accessibility",
    "test:performance": "performance_smoke",
    "test:perf": "performance_smoke",
    "check:licenses": "license",
    "license:check": "license",
    "audit": "vulnerability",
    "sbom": "sbom",
    "package": "package_release",
}

Commands = dict[str, list[str]]


def detect_node(commands: Commands) -> None:
    package_json = ROOT / "package.json"
    if not package_json.exists():
        return
    scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
    if (ROOT / "pnpm-lock.yaml").exists():
        runner, install = "pnpm run", "pnpm install --frozen-lockfile"
    elif (ROOT / "yarn.lock").exists():
        runner, install = "yarn", "yarn install --immutable"
    else:
        runner, install = "npm run", "npm ci"
    commands["bootstrap_local"].append(install)
    commands["bootstrap_ci"].append(install)
    for script, group in NODE_SCRIPT_GROUPS.items():
        if script in scripts:
            commands[group].append(f"yarn {script}" if runner == "yarn" else f"{runner} {script}")
    if (ROOT / "playwright.config.ts").exists() or (ROOT / "playwright.config.js").exists():
        commands["e2e"].append(
            "pnpm exec playwright test" if runner.startswith("pnpm") else "npx playwright test"
        )


def python_prefix(commands: Commands) -> str:
    """Record the Python bootstrap commands and return the runner prefix they imply."""
    if (ROOT / "uv.lock").exists():
        commands["bootstrap_local"].append("uv sync --all-extras --dev")
        commands["bootstrap_ci"].append("uv sync --frozen --all-extras --dev")
        return "uv run "
    if (ROOT / "poetry.lock").exists():
        commands["bootstrap_local"].append("poetry install --with dev")
        commands["bootstrap_ci"].append("poetry install --with dev --no-interaction")
        return "poetry run "
    if (ROOT / "requirements.txt").exists():
        commands["bootstrap_local"].append("python -m pip install -r requirements.txt")
        commands["bootstrap_ci"].append("python -m pip install -r requirements.txt")
    return ""


def detect_python(commands: Commands) -> None:
    if not any((ROOT / name).exists() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        return
    prefix = python_prefix(commands)
    if (ROOT / "ruff.toml").exists() or (ROOT / "pyproject.toml").exists():
        commands["format_check"].append(prefix + "ruff format --check .")
        commands["lint"].append(prefix + "ruff check .")
    if (ROOT / "mypy.ini").exists() or (ROOT / "pyproject.toml").exists():
        commands["typecheck"].append(prefix + "mypy .")
    if (ROOT / "tests").exists():
        commands["unit"].append(prefix + "pytest -q")
    if (ROOT / "pyproject.toml").exists() and (ROOT / "requirements.txt").exists():
        # Two files that can each declare dependencies is the shape Issue #16 removed.
        # Regenerating this config must not quietly drop the check that keeps it removed.
        commands["dependency_sync"].append('"$CI_PYTHON" workflow/check_dependencies.py')


def detect_rust(commands: Commands) -> None:
    if not (ROOT / "Cargo.toml").exists():
        return
    commands["format_check"].append("cargo fmt --all -- --check")
    commands["lint"].append("cargo clippy --workspace --all-targets --all-features -- -D warnings")
    commands["unit"].append("cargo test --workspace --all-features")
    commands["build"].append("cargo build --workspace --all-features --release")


def detect_go(commands: Commands) -> None:
    if not (ROOT / "go.mod").exists():
        return
    commands["format_check"].append('test -z "$(gofmt -l .)"')
    commands["lint"].append("go vet ./...")
    commands["unit"].append("go test ./...")
    commands["build"].append("go build ./...")


def detect_jvm(commands: Commands) -> None:
    if (ROOT / "gradlew").exists():
        commands["unit"].append("./gradlew test")
        commands["build"].append("./gradlew build")
    elif (ROOT / "pom.xml").exists():
        commands["unit"].append("mvn -B test")
        commands["build"].append("mvn -B package -DskipTests")


def detect_dotnet(commands: Commands) -> None:
    if not (list(ROOT.glob("*.sln")) or list(ROOT.rglob("*.csproj"))):
        return
    commands["bootstrap_ci"].append("dotnet restore")
    commands["format_check"].append("dotnet format --verify-no-changes")
    commands["unit"].append("dotnet test --no-restore")
    commands["build"].append("dotnet build --no-restore -c Release")


def detect_ruby(commands: Commands) -> None:
    if not (ROOT / "Gemfile").exists():
        return
    commands["bootstrap_local"].append("bundle install")
    commands["bootstrap_ci"].append("bundle install")
    commands["lint"].append("bundle exec rubocop")
    if (ROOT / "spec").exists():
        commands["unit"].append("bundle exec rspec")
    elif (ROOT / "test").exists():
        commands["unit"].append("bundle exec rake test")


def detect_docker(commands: Commands) -> None:
    if (ROOT / "Dockerfile").exists():
        commands["image_build"].append(
            'docker build --pull -t "local/${GITHUB_REPOSITORY:-project}:${GITHUB_SHA:-dev}" .'
        )


DETECTORS = (
    detect_node,
    detect_python,
    detect_rust,
    detect_go,
    detect_jvm,
    detect_dotnet,
    detect_ruby,
    detect_docker,
)


def detect_commands() -> dict[str, list[str]]:
    """Infer per-ecosystem command groups from the files present in the repository.

    Each detector appends freely; duplicates are collapsed once at the end, which
    is why the detectors themselves do not need membership checks.
    """
    commands: Commands = {group: [] for group in COMMAND_GROUPS}
    for detect in DETECTORS:
        detect(commands)
    return {key: list(dict.fromkeys(value)) for key, value in commands.items()}


def update_dependabot() -> None:
    ecosystems: list[tuple[str, list[str]]] = [
        ("github-actions", ["type:maintenance", "risk:dependencies", "risk:ci"])
    ]
    detections = (
        ("npm", ["package.json"]),
        ("pip", ["pyproject.toml", "requirements.txt", "setup.py"]),
        ("cargo", ["Cargo.toml"]),
        ("gomod", ["go.mod"]),
        ("maven", ["pom.xml"]),
        ("gradle", ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"]),
        ("bundler", ["Gemfile"]),
        ("docker", ["Dockerfile"]),
    )
    for ecosystem, manifests in detections:
        if any((ROOT / manifest).exists() for manifest in manifests):
            ecosystems.append((ecosystem, ["type:maintenance", "risk:dependencies"]))
    if (
        list(ROOT.glob("*.csproj"))
        or list(ROOT.glob("*.fsproj"))
        or (ROOT / "Directory.Packages.props").exists()
    ):
        ecosystems.append(("nuget", ["type:maintenance", "risk:dependencies"]))

    lines = ["version: 2", "updates:"]
    for ecosystem, labels in ecosystems:
        lines.extend(
            [
                f'  - package-ecosystem: "{ecosystem}"',
                '    directory: "/"',
                "    schedule:",
                '      interval: "weekly"',
                "    open-pull-requests-limit: 5",
                "    labels:",
                *[f'      - "{label}"' for label in labels],
            ]
        )
    (ROOT / ".github" / "dependabot.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_workflow_branches(
    old_integration: str, old_production: str, integration: str, production: str
) -> None:
    workflow_dir = ROOT / ".github" / "workflows"
    if not workflow_dir.exists():
        return
    for path in workflow_dir.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            f"branches: [{old_production}, {old_integration}]",
            f"branches: [{production}, {integration}]",
        )
        text = text.replace(
            f"branches: [{old_integration}, {old_production}]",
            f"branches: [{integration}, {production}]",
        )
        text = text.replace(f"branches: [{old_integration}]", f"branches: [{integration}]")
        text = text.replace(f"branches: [{old_production}]", f"branches: [{production}]")
        text = text.replace(
            f"github.ref == 'refs/heads/{old_integration}'", f"github.ref == 'refs/heads/{integration}'"
        )
        text = text.replace(
            f"github.ref == 'refs/heads/{old_production}'", f"github.ref == 'refs/heads/{production}'"
        )
        text = text.replace(
            f"github.event.workflow_run.head_branch == '{old_production}'",
            f"github.event.workflow_run.head_branch == '{production}'",
        )
        text = text.replace(f"origin/{old_production}", f"origin/{production}")
        text = text.replace(f"origin {old_production}", f"origin {production}")
        text = text.replace(f"refs/heads/{old_production}", f"refs/heads/{production}")
        path.write_text(text, encoding="utf-8")


def cmd_init(args: argparse.Namespace) -> int:
    config = load_config()
    old_integration = config["branches"]["integration"]
    old_production = config["branches"]["production"]
    config["branches"] = {"integration": args.integration_branch, "production": args.production_branch}
    config["quality"]["base_ref"] = f"origin/{args.integration_branch}"
    detected = detect_commands()
    for group, commands in detected.items():
        if commands:
            config["commands"][group] = commands
    save_config(config)
    update_workflow_branches(old_integration, old_production, args.integration_branch, args.production_branch)
    update_dependabot()

    gitignore = ROOT / ".gitignore"
    addition = (ROOT / ".gitignore.claude-ci").read_text(encoding="utf-8")
    current = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if "# Claude Code CI workflow local state" not in current:
        gitignore.write_text(current.rstrip() + "\n\n" + addition, encoding="utf-8")

    print("Initialized .claude-workflow.json with detected project commands.")
    print("Review every command before trusting the repository in Claude Code.")
    return 0


def cmd_detect(_: argparse.Namespace) -> int:
    print(json.dumps(detect_commands(), indent=2))
    return 0


def stage_command(stage: str) -> list[str]:
    """How to run a CI stage from Python.

    Not `ci/run`: that is a bash shim with no extension, and Windows CreateProcess does not
    consult shebangs, so executing it raises WinError 193 before any gate runs. The shim's
    only job is resolving an interpreter, and `sys.executable` is already the one the `flow`
    shim resolved, so calling the stage runner's Python entry point keeps the same runtime
    and works on both platforms.
    """
    return [sys.executable, str(ROOT / "ci" / "run.py"), stage]


def venv_tool(venv: Path, tool: str) -> Path | None:
    """Locate a venv-installed tool in either layout: Windows `Scripts/`, POSIX `bin/`."""
    for relative in (f"Scripts/{tool}.exe", f"bin/{tool}"):
        candidate = venv / relative
        if candidate.exists():
            return candidate
    return None


def resolved_python() -> str:
    """The interpreter the bash entry points would select, or "" when none works.

    Checking `command -v python3` instead reports success for the Microsoft Store stub,
    which sits on PATH and exits without running anything. Deferring to resolve_python
    means doctor validates the path the scripts actually take, including its requirement
    that a candidate execute before being accepted.
    """
    library = ROOT / "scripts" / "lib" / "python.sh"
    # --noprofile --norc, not -l: a login shell sources profiles, and anything they print
    # lands in the captured value, so doctor would report a banner as an interpreter path.
    # The entry points source python.sh directly, so this is also the faithful comparison.
    candidate = output(
        [
            bash_command(),
            "--noprofile",
            "--norc",
            "-c",
            f". {shlex.quote(str(library))} && resolve_python",
        ]
    )
    if not candidate:
        return ""
    probe = shell(
        [candidate, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
        check=False,
        capture=True,
    )
    return candidate if probe.returncode == 0 else ""


def ci_home_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CI_HOME", Path.home() / ".local/share/claude-code-ci/v2"))


def doctor_environment(interpreter: str) -> tuple[list[str], list[str]]:
    """Toolchain findings: what must be present, and what merely should be."""
    failures: list[str] = []
    warnings: list[str] = []
    if not output([bash_command(), "-lc", "command -v git"]):
        failures.append("missing required tool: git")
    if not interpreter:
        failures.append(
            "no working Python 3.10+ interpreter resolves; run ./scripts/bootstrap or set PYTHON_BIN"
        )
    for tool in ["gh", "claude", "docker"]:
        if not output([bash_command(), "-lc", f"command -v {shlex.quote(tool)}"]):
            warnings.append(f"optional tool unavailable: {tool}")
    if not venv_tool(ci_home_dir() / "venv", "pre-commit"):
        warnings.append("global CI tool venv is missing; run ./scripts/bootstrap")
    return failures, warnings


def doctor_repository(config: dict[str, Any]) -> list[str]:
    """Findings about the repository's own configuration and entry points."""
    failures: list[str] = []
    for branch_type, branch in config["branches"].items():
        if not output(["git", "check-ref-format", "--branch", branch]):
            failures.append(f"invalid {branch_type} branch name: {branch}")
    for script in ["flow", "ci/run", "scripts/bootstrap", "scripts/setup-github"]:
        path = ROOT / script
        if not path.exists() or not os.access(path, os.X_OK):
            failures.append(f"missing or non-executable: {script}")
    return failures


def cmd_doctor(_: argparse.Namespace) -> int:
    config = load_config()
    interpreter = resolved_python()
    failures, warnings = doctor_environment(interpreter)
    failures += doctor_repository(config)
    print("Repository:", ROOT)
    print("Interpreter:", interpreter or "unresolved")
    print("CI runtime:", ci_home_dir() / "venv")
    print("Integration branch:", config["branches"]["integration"])
    print("Production branch:", config["branches"]["production"])
    print("Configured stages:")
    for stage, groups in config["stages"].items():
        print(f"  {stage}: {', '.join(groups)}")
    for warning in warnings:
        print("WARNING:", warning)
    for failure in failures:
        print("FAIL:", failure)
    return 1 if failures else 0


def require_gh() -> None:
    if not gh_ready():
        fail("GitHub CLI authentication is required; run gh auth login")


def cmd_sync_control(_: argparse.Namespace) -> int:
    require_gh()
    sync_control()
    return 0


def set_issue_state(issue: int, state: str, config: dict[str, Any]) -> None:
    raw = output(["gh", "issue", "view", str(issue), "--json", "labels"])
    current = {str(item.get("name")) for item in json.loads(raw or "{}").get("labels", [])} if raw else set()
    command = ["gh", "issue", "edit", str(issue)]
    for label in config.get("github", {}).get("state_labels", []):
        if label in current and label != state:
            command.extend(["--remove-label", str(label)])
    if state not in current:
        command.extend(["--add-label", state])
    if len(command) > 4:
        shell(command, check=False)


def copy_risk_labels_to_pr(issue: int, pr_number: int) -> None:
    raw = output(["gh", "issue", "view", str(issue), "--json", "labels"])
    labels = json.loads(raw or "{}").get("labels", []) if raw else []
    risks = sorted(str(item.get("name")) for item in labels if str(item.get("name", "")).startswith("risk:"))
    if not risks:
        return
    command = ["gh", "pr", "edit", str(pr_number)]
    for label in risks:
        command.extend(["--add-label", label])
    shell(command, check=False)


def cmd_new(args: argparse.Namespace) -> int:
    require_gh()
    config = load_config()
    issue = json.loads(
        output(["gh", "issue", "view", str(args.issue), "--json", "number,title,url,state"]) or "{}"
    )
    if not issue:
        fail(f"Issue #{args.issue} was not found")
    if issue.get("state") != "OPEN":
        fail(f"Issue #{args.issue} is not open")
    base = args.base or config["branches"]["integration"]
    slug = args.slug or slugify(issue["title"])
    branch = f"{config['worktrees']['branch_prefix']}/{args.issue}-{slug}"
    worktree_root = (ROOT / config["worktrees"]["root"]).resolve()
    worktree = worktree_root / f"{args.issue}-{slug}"
    worktree_root.mkdir(parents=True, exist_ok=True)
    shell(["git", "fetch", "origin", base])
    if output(["git", "show-ref", "--verify", f"refs/heads/{branch}"]):
        shell(["git", "worktree", "add", str(worktree), branch])
    else:
        shell(["git", "worktree", "add", "-b", branch, str(worktree), f"origin/{base}"])
    set_issue_state(args.issue, "state:active", config)
    shell(
        [
            "gh",
            "issue",
            "comment",
            str(args.issue),
            "--body",
            f"Implementation started on branch `{branch}` in an isolated worktree.",
        ],
        check=False,
    )
    sync_control(config)
    print(f"\nWorktree: {worktree}\nBranch: {branch}")
    if args.launch:
        os.chdir(worktree)
        os.execvp("claude", ["claude", "--profile", "interactive"])
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    require_gh()
    branch = output(["git", "branch", "--show-current"]) or "detached"
    inferred = issue_number_from_branch(branch)
    issue = args.issue or inferred
    if not issue:
        fail("provide an Issue number or use a branch containing one")
    config = load_config()
    control = find_control_issue(config)
    if control:
        print("--- pinned repository state ---")
        print(output(["gh", "issue", "view", str(control["number"])]))
        print("\n--- controlling task Issue ---")
    print(output(["gh", "issue", "view", str(issue)]))
    print("\n--- repository state ---")
    shell(["git", "status", "--short", "--branch"], check=False)
    shell(["git", "log", "--oneline", "-8"], check=False)
    prs = output(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,title,state,url,reviewDecision,statusCheckRollup",
        ]
    )
    print("\n--- pull request state ---")
    print(prs or "No pull request found for this branch.")
    return 0


def validate_state(text: str) -> None:
    missing = [heading for heading in REQUIRED_STATE_HEADINGS if heading not in text]
    if missing:
        fail("state file is missing headings: " + ", ".join(missing))


def managed_block(state: str) -> str:
    return f"{STATE_START}\n{state.strip()}\n{STATE_END}"


def replace_managed_block(body: str, replacement: str) -> str:
    pattern = re.compile(re.escape(STATE_START) + r".*?" + re.escape(STATE_END), re.DOTALL)
    if pattern.search(body):
        # A function replacement, never the string itself: re.sub reads a string
        # replacement as a template, so a backslash in handoff state is expanded rather
        # than kept. A Windows path such as F:\PROJECTs raised `bad escape \P` and aborted
        # the handoff outright; `\1` would have silently substituted a capture group.
        return pattern.sub(lambda _: replacement, body)
    return body.rstrip() + "\n\n" + replacement + "\n"


def replace_control_block(body: str, replacement: str) -> str:
    pattern = re.compile(re.escape(CONTROL_START) + r".*?" + re.escape(CONTROL_END), re.DOTALL)
    if pattern.search(body):
        return pattern.sub(lambda _: replacement, body)
    if body.strip():
        return replacement + "\n\n## Previous control notes\n\n" + body.strip() + "\n"
    return replacement + "\n\n## Current hazards\n\nNone recorded.\n"


def labels_for(item: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in item.get("labels", [])}


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def body_section(body: str, heading: str, limit: int = 1400) -> str:
    match = re.search(rf"(?ims)^##+\s+{re.escape(heading)}\s*$\n(.*?)(?=^##+\s|\Z)", body or "")
    if not match:
        return ""
    return re.sub(r"\n{3,}", "\n\n", match.group(1).strip())[:limit].rstrip()


def check_summary(rollup: list[dict[str, Any]]) -> str:
    if not rollup:
        return "not run"
    values = {
        str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        for item in rollup
    }
    if values & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}:
        return "failing"
    if values & {"PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED", "WAITING", "EXPECTED"}:
        return "pending"
    if values <= {"SUCCESS", "SKIPPED", "NEUTRAL"}:
        return "passing"
    return "mixed"


def find_control_issue(config: dict[str, Any]) -> dict[str, Any]:
    title = config["github"]["control_issue_title"]
    raw = output(["gh", "issue", "list", "--state", "open", "--limit", "200", "--json", "number,title,url"])
    issues = json.loads(raw or "[]")
    return next((item for item in issues if item.get("title") == title), {})


def remote_branch_sha(branch: str) -> str:
    value = output(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/branches/{quote(branch, safe='')}", "--jq", ".commit.sha"]
    )
    return value[:12] if value else "unavailable"


ACTIVE_STATES = {"state:active", "state:blocked", "state:review", "state:release-ready"}


def open_issues_and_prs() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Fetch open Issues and open PRs, keyed by the Issue number in the PR branch name."""
    raw_issues = output(
        ["gh", "issue", "list", "--state", "open", "--limit", "200", "--json", "number,title,labels,url"]
    )
    raw_prs = output(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title,headRefName,baseRefName,url,statusCheckRollup",
        ]
    )
    pr_by_issue: dict[int, dict[str, Any]] = {}
    for pr in json.loads(raw_prs or "[]"):
        inferred = issue_number_from_branch(str(pr.get("headRefName", "")))
        if inferred is not None:
            pr_by_issue[inferred] = pr
    return json.loads(raw_issues or "[]"), pr_by_issue


def active_row(issue: dict[str, Any], pr: dict[str, Any] | None, state_labels: list[str]) -> str:
    labels = labels_for(issue)
    branch = str(pr.get("headRefName", "—")) if pr else "—"
    state = ", ".join(label.removeprefix("state:") for label in state_labels) or "PR open"
    risks = (
        ", ".join(sorted(label.removeprefix("risk:") for label in labels if label.startswith("risk:"))) or "—"
    )
    pr_cell = (
        f"[#{pr['number']}]({pr['url']}) / {check_summary(pr.get('statusCheckRollup', []))}" if pr else "—"
    )
    return (
        f"| [#{issue['number']}]({issue['url']}) {markdown_cell(str(issue['title']))} "
        f"| `{markdown_cell(branch)}` | {markdown_cell(state)} | {markdown_cell(risks)} | {pr_cell} |"
    )


def collect_active(
    issues: list[dict[str, Any]], pr_by_issue: dict[int, dict[str, Any]], control_number: Any
) -> tuple[list[str], list[dict[str, Any]]]:
    rows: list[str] = []
    release_issues: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("number") == control_number:
            continue
        labels = labels_for(issue)
        if "type:release" in labels:
            release_issues.append(issue)
        pr = pr_by_issue.get(int(issue["number"]))
        state_labels = sorted(labels & ACTIVE_STATES)
        if not state_labels and pr is None:
            continue
        rows.append(active_row(issue, pr, state_labels))
    return rows, release_issues


def render_control_block(
    config: dict[str, Any], rows: list[str], release_issues: list[dict[str, Any]]
) -> str:
    current_release = max(release_issues, key=lambda item: int(item["number"])) if release_issues else None
    release_text = (
        f"[#{current_release['number']}]({current_release['url']}) {markdown_cell(str(current_release['title']))}"
        if current_release
        else "none"
    )
    row_limit = int(config.get("tracking", {}).get("control_issue_max_active_rows", 20))
    overflow = max(0, len(rows) - row_limit)
    table = "\n".join(rows[:row_limit]) if rows[:row_limit] else "| — | — | no active task Issues | — | — |"
    if overflow:
        table += f"\n\n_{overflow} additional active Issue(s) omitted; use the `state:*` filters for the full list._"
    production = config["branches"]["production"]
    integration = config["branches"]["integration"]
    return f"""{CONTROL_START}
## Branch state

- Production: `{production}@{remote_branch_sha(production)}`
- Integration: `{integration}@{remote_branch_sha(integration)}`
- Current release Issue: {release_text}

## Active work

| Issue | Branch | State | Risk | PR / CI |
|---|---|---|---|---|
{table}

_Last synchronized by `./flow sync-control`._
{CONTROL_END}"""


def guard_lossy_body(label: str, body: str, original: str | None) -> None:
    """Refuse to persist text that this tool made lossy.

    Reads decode with errors="replace" so an undecodable byte cannot kill the command, but
    that turns a lossy read into a silent write, and an Issue body is handoff truth. A
    replacement character that was not there before means information was lost on the way
    in, so this stops rather than persisting the loss.

    Text that already carried U+FFFD keeps the right to be written back. Refusing that too
    would leave a body corrupted by the pre-#35 code permanently unrepairable, and would let
    one stray character in an unrelated Issue title -- the control body is composed from
    every active Issue's title -- wedge every control-plane command with no way out.
    Counting is deliberately coarse: it cannot tell a moved replacement character from a
    preserved one, and errs toward allowing the write it can prove is not lossier.
    """
    already = (original or "").count(REPLACEMENT)
    carried = body.count(REPLACEMENT)
    if carried > already:
        fail(
            f"refusing to write {label}: {carried - already} character(s) could not be decoded "
            "on the way in. Inspect the source before rerunning."
        )
    if carried:
        print(
            f"warning: {label} already carried {carried} replacement character(s); this write "
            "preserves them rather than introducing them.",
            file=sys.stderr,
        )


def write_with_body_file(
    command: list[str], body: str, *, label: str, prefix: str, original: str | None = None
) -> None:
    """Run a `gh` command with `body` supplied through a UTF-8 file instead of argv.

    newline="" matters as much as the codec. Text mode translates every LF to CRLF on
    Windows, so the body GitHub stored would not be the body that was read, and each cycle
    would add another CR -- the same silent corruption of handoff truth as the locale codec,
    reintroduced by the fix for it (Issue #35).
    """
    guard_lossy_body(label, body, original)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", prefix=prefix, suffix=".md", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(body)
        shell([*command, "--body-file", str(temporary)])
    finally:
        temporary.unlink(missing_ok=True)


def write_issue_body(number: Any, body: str, *, prefix: str, original: str | None = None) -> None:
    write_with_body_file(
        ["gh", "issue", "edit", str(number)],
        body,
        label=f"Issue #{number}",
        prefix=prefix,
        original=original,
    )


def write_control_body(number: Any, updated: str, original: str | None = None) -> None:
    write_issue_body(number, updated, prefix="claude-control-", original=original)


def sync_control(config: dict[str, Any] | None = None) -> None:
    config = config or load_config()
    control = find_control_issue(config)
    if not control:
        fail("the pinned control Issue was not found; run ./scripts/setup-github")

    issues, pr_by_issue = open_issues_and_prs()
    rows, release_issues = collect_active(issues, pr_by_issue, control.get("number"))
    managed = render_control_block(config, rows, release_issues)

    issue_json = output(["gh", "issue", "view", str(control["number"]), "--json", "body"])
    if not issue_json:
        fail(f"unable to read control Issue #{control['number']}")
    body = json.loads(issue_json).get("body") or ""
    updated = replace_control_block(body, managed)
    if updated == body:
        print(f"Control Issue #{control['number']} is already current.")
        return
    write_control_body(control["number"], updated, original=body)
    print(f"Control Issue #{control['number']} synchronized.")


def machine_checkpoint(issue: int) -> str:
    branch_name = output(["git", "branch", "--show-current"]) or "detached"
    commit = output(["git", "rev-parse", "HEAD"]) or "unknown"
    dirty = bool(output(["git", "status", "--porcelain"]))
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return f"""## Machine checkpoint

- Issue: #{issue}
- Branch: `{branch_name}`
- Commit: `{commit}`
- Working tree: {"dirty" if dirty else "clean"}
- Updated: `{timestamp}`
"""


def issue_snapshot(issue: int) -> dict[str, Any]:
    raw = output(["gh", "issue", "view", str(issue), "--json", "body,updatedAt,state,url"])
    if not raw:
        fail(f"unable to read Issue #{issue}")
    value = json.loads(raw)
    if value.get("state") != "OPEN":
        fail(f"Issue #{issue} is not open")
    return value


def cmd_handoff(args: argparse.Namespace) -> int:
    require_gh()
    branch_name = output(["git", "branch", "--show-current"]) or "detached"
    inferred = issue_number_from_branch(branch_name)
    if inferred is not None and inferred != args.issue:
        fail(f"branch implies Issue #{inferred}, not #{args.issue}")
    state_path = Path(args.state_file).expanduser()
    if not state_path.is_file():
        fail(f"state file does not exist: {state_path}")
    state = state_path.read_text(encoding="utf-8")
    validate_state(state)
    first = issue_snapshot(args.issue)
    body = first.get("body") or ""
    state_with_checkpoint = state.rstrip() + "\n\n" + machine_checkpoint(args.issue)
    updated = replace_managed_block(body, managed_block(state_with_checkpoint))

    # GitHub Issue edits do not expose a compare-and-swap primitive through gh issue edit.
    # Re-read immediately before writing and refuse to overwrite a concurrent session.
    second = issue_snapshot(args.issue)
    if second.get("updatedAt") != first.get("updatedAt") or (second.get("body") or "") != body:
        fail(
            f"Issue #{args.issue} changed while preparing the handoff. "
            "Re-read it, reconcile the other session's update, and rerun ./flow handoff."
        )

    write_issue_body(args.issue, updated, prefix=f"claude-issue-{args.issue}-", original=body)

    verified = issue_snapshot(args.issue)
    # Compare content, not line-ending convention: GitHub may hand back CRLF for a body that
    # was uploaded with LF, and that difference is transport, not loss.
    stored = (verified.get("body") or "").replace("\r\n", "\n")
    if managed_block(state_with_checkpoint).replace("\r\n", "\n") not in stored:
        fail(f"Issue #{args.issue} did not retain the expected managed handoff block")
    print(
        f"Issue #{args.issue} now contains current handoff state for commit {output(['git', 'rev-parse', '--short=12', 'HEAD']) or 'unknown'}."
    )
    release_local_lease(args.issue)
    print(
        f"Released the local work lease for Issue #{args.issue}; the next session can resume from the handoff."
    )
    sync_control()
    return 0


def require_task_branch(issue: int, config: dict[str, Any]) -> str:
    """Assert the current branch is a task branch for `issue` and the tree is clean."""
    branch = output(["git", "branch", "--show-current"])
    if not branch or branch == "HEAD":
        fail("a named task branch is required")
    if branch in config["branches"].values():
        fail("open task PRs only from a task branch, never from an integration or production branch")
    inferred = issue_number_from_branch(branch)
    if inferred is None:
        fail("task branch name must contain its controlling Issue number")
    if inferred != issue:
        fail(f"branch implies Issue #{inferred}, not #{issue}")
    if output(["git", "status", "--porcelain"]):
        fail("working tree is dirty; commit or intentionally discard changes before opening/updating the PR")
    return branch


def compose_pr_body(issue_number: int, issue: dict[str, Any]) -> str:
    """Build a PR body carrying every section workflow/validate_pr.py requires."""
    issue_body = str(issue.get("body") or "")
    result_text = (
        body_section(issue_body, "Current implementation state")
        or body_section(issue_body, "Objective")
        or "Implements the controlling Issue acceptance criteria."
    )
    implementation_text = (
        "\n\n".join(
            part
            for part in (body_section(issue_body, "Decisions"), body_section(issue_body, "Changed areas"))
            if part
        )
        or "See the focused diff and controlling Issue for implementation details."
    )
    risk_labels = sorted(
        str(item.get("name"))
        for item in issue.get("labels", [])
        if str(item.get("name", "")).startswith("risk:")
    )
    risk_text = (
        ", ".join(f"`{label}`" for label in risk_labels)
        or "No elevated risk label is currently assigned; changed-path validation remains authoritative."
    )
    remaining = body_section(issue_body, "Known problems") or "None known."
    return f"Refs #{issue_number}\n\n## Result\n\n{result_text}\n\n## Implementation\n\n{implementation_text}\n\n## Verification\n\n- [x] `./ci/run fast`\n- [x] `./ci/run pr`\n- [ ] Required GitHub checks\n\n## Risk\n\n{risk_text}\n\n## Remaining work\n\n{remaining}\n"


def open_pr_for_branch(issue_number: int, branch: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return the open PR for `branch`, creating it when one does not already exist."""
    existing = output(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number,url"])
    if existing and json.loads(existing):
        pr: dict[str, Any] = json.loads(existing)[0]
        print(f"Pull request already open: {pr['url']}")
        return pr
    # `output()` returns "" for a failed command, and this runs after the push: an unguarded
    # json.loads would raise JSONDecodeError with the branch already published and no PR.
    issue = json.loads(
        output(["gh", "issue", "view", str(issue_number), "--json", "title,body,labels"]) or "{}"
    )
    if not issue:
        fail(f"unable to read Issue #{issue_number}; the branch is pushed but no PR was opened")
    write_with_body_file(
        [
            "gh",
            "pr",
            "create",
            "--base",
            config["branches"]["integration"],
            "--title",
            f"#{issue_number}: {issue['title']}",
        ],
        compose_pr_body(issue_number, issue),
        label=f"the pull request for Issue #{issue_number}",
        prefix=f"claude-pr-{issue_number}-",
        original=str(issue.get("body") or ""),
    )
    created = json.loads(
        output(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--limit", "1", "--json", "number,url"]
        )
        or "[]"
    )
    if not created:
        fail("pull request creation succeeded but the new PR could not be resolved")
    return created[0]


def cmd_pr(args: argparse.Namespace) -> int:
    require_gh()
    config = load_config()
    branch = require_task_branch(args.issue, config)
    shell(stage_command("fast"))
    shell(stage_command("pr"))
    shell(["git", "push", "--set-upstream", "origin", branch])
    pr = open_pr_for_branch(args.issue, branch, config)
    copy_risk_labels_to_pr(args.issue, int(pr["number"]))
    set_issue_state(args.issue, "state:review", config)
    sync_control(config)
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    require_gh()
    config = load_config()
    integration = config["branches"]["integration"]
    production = config["branches"]["production"]
    issue = json.loads(
        output(["gh", "issue", "view", str(args.issue), "--json", "title,body,labels,state"]) or "{}"
    )
    if not issue:
        fail(f"unable to read release Issue #{args.issue}")
    issue_labels = {str(item.get("name")) for item in issue.get("labels", [])}
    if str(issue.get("state", "")).upper() != "OPEN":
        fail(f"release Issue #{args.issue} must remain open")
    if "type:release" not in issue_labels:
        fail(f"Issue #{args.issue} must carry `type:release` before opening a production PR")
    shell(["git", "fetch", "origin", integration, production])
    existing = output(
        [
            "gh",
            "pr",
            "list",
            "--head",
            integration,
            "--base",
            production,
            "--state",
            "open",
            "--json",
            "number,url",
        ]
    )
    if existing and json.loads(existing):
        pr = json.loads(existing)[0]
        print(f"Release pull request already open: {pr['url']}")
    else:
        title = f"{config['github']['release_title_prefix']} {issue['title']}"
        write_with_body_file(
            [
                "gh",
                "pr",
                "create",
                "--head",
                integration,
                "--base",
                production,
                "--title",
                title,
            ],
            f"Refs #{args.issue}\n\nRelease candidate from `{integration}` to `{production}`.\n\n## Release verification\n\n- [ ] Full regression\n- [ ] Migration matrix\n- [ ] Production image browser tests\n- [ ] Artifact manifest and attestations\n- [ ] Deployment approval\n",
            label=f"the release pull request for Issue #{args.issue}",
            prefix=f"claude-release-{args.issue}-",
        )
        created = json.loads(
            output(
                [
                    "gh",
                    "pr",
                    "list",
                    "--head",
                    integration,
                    "--base",
                    production,
                    "--state",
                    "open",
                    "--limit",
                    "1",
                    "--json",
                    "number,url",
                ]
            )
            or "[]"
        )
        if not created:
            fail("release PR creation succeeded but the PR could not be resolved")
        pr = created[0]
    copy_risk_labels_to_pr(args.issue, int(pr["number"]))
    set_issue_state(args.issue, "state:review", config)
    sync_control(config)
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    command = [sys.executable, str(ROOT / "workflow" / "local_metrics.py"), "--days", str(args.days)]
    if args.json:
        command.append("--json")
    return subprocess.run(command, cwd=ROOT).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue/PR/CI workflow controller")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--integration-branch", default="dev")
    init.add_argument("--production-branch", default="master")
    init.set_defaults(func=cmd_init)
    detect = sub.add_parser("detect")
    detect.set_defaults(func=cmd_detect)
    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=cmd_doctor)
    sync = sub.add_parser("sync-control")
    sync.set_defaults(func=cmd_sync_control)
    new = sub.add_parser("new")
    new.add_argument("issue", type=int)
    new.add_argument("--slug")
    new.add_argument("--base")
    new.add_argument("--launch", action="store_true")
    new.set_defaults(func=cmd_new)
    start = sub.add_parser("start")
    start.add_argument("issue", type=int, nargs="?")
    start.set_defaults(func=cmd_start)
    handoff = sub.add_parser("handoff")
    handoff.add_argument("issue", type=int)
    handoff.add_argument("--state-file", required=True)
    handoff.set_defaults(func=cmd_handoff)
    pr = sub.add_parser("pr")
    pr.add_argument("issue", type=int)
    pr.set_defaults(func=cmd_pr)
    release = sub.add_parser("release")
    release.add_argument("issue", type=int)
    release.set_defaults(func=cmd_release)
    metrics = sub.add_parser("metrics")
    metrics.add_argument("--days", type=int, default=7)
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=cmd_metrics)
    return parser


def main() -> int:
    use_utf8_streams()
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
