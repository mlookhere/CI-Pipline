#!/usr/bin/env python3
from __future__ import annotations

import base64
import fnmatch
import os
import re
import shlex

from common import *

DENY = [
    (r"\bgit\s+(?:commit|push)\b[^\n]*--no-verify\b", "Hook bypasses are prohibited; fix the failing hook."),
    (
        r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease|-if-includes)?(?:=\S+)?(?:\s|$)|-f(?:\s|$))",
        "Force-pushing is prohibited by the standard workflow.",
    ),
    (r"\bgh\s+pr\s+merge\b[^\n]*--admin\b", "Administrator merge bypasses are prohibited."),
    (r"\bgit\s+reset\s+--hard\b", "Hard reset is prohibited in a standard task session."),
    (r"\bgit\s+clean\b[^\n]*(?:-[A-Za-z]*[fx][A-Za-z]*|--force)", "Destructive git clean is prohibited."),
    (r"\brm\s+-rf\s+(?:/|~|\$HOME|\.git)(?:\s|$)", "Destructive filesystem deletion is prohibited."),
    # The same prohibition in PowerShell, which spells it as a cmdlet with separate
    # switches rather than a bundled `-rf`, so the entry above never saw it. The target
    # carries the decision here instead of the switches: `-Recurse` may be abbreviated to
    # any prefix and may sit on either side of the path, while deleting a drive root, a
    # home directory or `.git` is catastrophic with or without it.
    (
        r"\b(?:Remove-Item|ri|rm|rmdir|rd|del|erase)\b[^\n|;]*?(?:\s|['\"])(?:/|[A-Za-z]:[\\/]?|~|\$HOME|\$env:USERPROFILE|\.git)[\\/]?(?=[\s'\"]|$)",
        "Destructive filesystem deletion is prohibited.",
    ),
    (r"\bdocker\s+system\s+prune\b", "Docker system pruning is prohibited in an agent session."),
    (
        r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash|zsh)\b",
        "Piping remote content directly into a shell is prohibited.",
    ),
    # PowerShell's spelling of the entry above: nothing here reaches `sh`, `bash` or `zsh`
    # for that pattern to match. Both orders are refused because `iex (iwr $url)` is as
    # common as the pipeline form.
    (
        r"\b(?:Invoke-WebRequest|Invoke-RestMethod|iwr|irm|curl|wget)\b[^\n]*\|\s*(?:Invoke-Expression|iex)\b"
        r"|\b(?:Invoke-Expression|iex)\b[^\n]*\b(?:Invoke-WebRequest|Invoke-RestMethod|iwr|irm|curl|wget)\b",
        "Piping remote content directly into a shell is prohibited.",
    ),
    (r"\bchmod\s+(?:-R\s+)?777\b", "World-writable permissions are prohibited."),
    (
        r"\bgit\s+config\b[^\n]*(?:http\..*extraheader|credential\.helper)",
        "Credential persistence through Git config is prohibited.",
    ),
]

NOISY = re.compile(
    r"(?i)(?:pytest|npm\s+(?:test|run\s+test)|pnpm\s+(?:test|run\s+test)|yarn\s+(?:test|run\s+test)|cargo\s+(?:test|clippy|build)|go\s+test|dotnet\s+test|mvn\s+.*test|gradle\w*\s+.*test|playwright\s+test|gh\s+run\s+view.*--log)"
)


def patch_paths(command: str) -> list[str]:
    return re.findall(r"(?m)^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", command or "")


def risk_matches(cfg: dict, paths: list[str]) -> dict[str, tuple[str, str]]:
    """Each required risk label, with the path and glob that first required it.

    The evidence is kept rather than just the label because the globs are deliberately
    wide: `**/*permission*` matches any path with that substring anywhere, and for Bash the
    "paths" are tokens scraped out of a command line, so a read-only grep for the word
    security is enough to require a label. A refusal that names only the label leaves the
    developer to guess which of a dozen tokens tripped it.
    """
    result: dict[str, tuple[str, str]] = {}
    for label, patterns in cfg.get("github", {}).get("risk_paths", {}).items():
        for path in paths:
            for pattern in patterns:
                if fnmatch.fnmatch(path, pattern):
                    result.setdefault(label, (path, pattern))
                    break
            if label in result:
                break
    return result


def protected_push(root: Path, command: str) -> bool:
    branches = list(config(root).get("branches", {}).values()) or ["main", "master", "dev"]
    if not re.search(r"(?i)\bgit\s+push\b", command):
        return False
    if branch(root) in branches:
        return True
    return any(re.search(rf"(?:\s|:){re.escape(name)}(?:\s|$)", command) for name in branches)


def deny(root: Path, event: dict, issue_no: int | None, reason: str, category: str) -> None:
    log_event(
        root,
        "PolicyDecision",
        {
            "session_id": event.get("session_id"),
            "issue": issue_no,
            "decision": "deny",
            "category": category,
        },
    )
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


# Paths worth a second look wherever they appear in a command line, including inside an
# argument this hook makes no attempt to parse. Either separator: PowerShell writes
# `.github\workflows\ci-pr.yml` and that is the same file.
RISK_PATH_HINTS = (
    r"(?:^|\s)(\.github[\\/](?:workflows|actions)[\\/]\S+"
    r"|[^\s]*(?:migration|schema|auth|security|deploy)[^\s]*)"
)
# The verbs that make what follows them a file the command is about to change, rather than
# one it happens to name. The hints above only know a handful of words, so they see
# `.github/workflows/ci-pr.yml` and miss `ci/run`; a token a write verb is aimed at is
# judged against every risk glob instead, which is what the Issue's own example --
# `Set-Content ci/run` -- needs. One pattern covers both shells: redirection is spelled the
# same in each, and a PowerShell session reaches for Set-Content where bash reaches for
# `tee`. Reads are deliberately absent: Get-Content of a risk path changes nothing.
WRITE_VERBS = re.compile(
    r">>?|\b(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Tee-Object|tee)\b", re.I
)
STATEMENT_END = re.compile(r"[;|\n]")


def lease_conflict(
    root: Path, cfg: dict, tool: str, command: str, issue_no: int | None, session_id: str
) -> str | None:
    """Reason to block, when another live session owns this Issue."""
    if tool not in WRITE_TOOLS and not mutation_command(command):
        return None
    ttl = int(cfg.get("tracking", {}).get("lease_ttl_seconds", 28800))
    if not foreign_lease(root, issue_no, session_id, ttl):
        return None
    return (
        f"Issue #{issue_no} has a live lease owned by another Claude Code session. "
        f"Run ./scripts/claude-lease release {issue_no} only after confirming the "
        "other session is stopped."
    )


def command_violation(root: Path, command: str) -> str | None:
    for pattern, reason in DENY:
        if re.search(pattern, command, re.I):
            return reason
    if protected_push(root, command):
        return "Direct pushes to integration or production branches are prohibited; use a pull request."
    return None


def plain_spelling(value: str) -> str:
    r"""`value` with Windows' extended-length or device prefix removed.

    `\\?\` exists to switch off path normalisation, and it survives Path.resolve(): the
    anchor stays `\\?\F:\`, so any containment test against `F:\...` sees two unrelated
    roots and gives up. `\\?\UNC\host\share` is the same trick for a network path. Both
    name exactly the file the plain spelling names, so they come off before anything
    judges the path -- otherwise `Write` to `\\?\<repo>\ci\run` is simply not seen.
    """
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if value.startswith(prefix):
            rest = value[len(prefix) :]
            return "\\\\" + rest[4:] if rest[:4].lower() == "unc\\" else rest
    return value


def contained_path(base: Path, target: Path) -> str | None:
    """`target` relative to `base` in POSIX form, or None when it is not underneath.

    os.path.relpath rather than Path.relative_to: it compares through os.path.normcase, so
    a drive letter or directory named in a different case still matches on Windows, and it
    refuses outright when the two are on different mounts instead of quietly disagreeing.
    """
    try:
        relative = os.path.relpath(target, base)
    except (OSError, ValueError):
        return None
    if relative in (os.curdir, os.pardir) or relative.startswith(os.pardir + os.sep):
        return None
    return relative.replace(os.sep, "/")


CHECKOUT_CACHE: dict[str, tuple[Path, Path] | None] = {}


def probe_checkout(directory: Path) -> tuple[Path, Path] | None:
    result = run(["git", "rev-parse", "--show-toplevel", "--git-common-dir"], cwd=directory)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) != 2:
        return None
    common = Path(lines[1])
    # git reports --git-common-dir relative to the directory it was asked from, so the
    # answer is meaningless without that directory to resolve it against.
    return Path(lines[0]), common if common.is_absolute() else directory / common


def checkout_of(path: Path) -> tuple[Path, Path] | None:
    """Work tree root and shared git directory of the checkout holding `path`, or None.

    Memoised per directory because this hook runs before every tool call and the probe is
    a process. Nothing reaches it for an edit inside the current checkout -- the caller
    answers those without leaving Python, which is nearly all of them.
    """
    directory = path if path.is_dir() else path.parent
    while not directory.is_dir() and directory.parent != directory:
        directory = directory.parent
    key = os.path.normcase(str(directory))
    if key not in CHECKOUT_CACHE:
        CHECKOUT_CACHE[key] = probe_checkout(directory)
    return CHECKOUT_CACHE[key]


def repo_relative(root: Path, value: str) -> str | None:
    """`value` as the repo-relative POSIX path the risk globs are written against.

    A write tool reports the file it is about to touch as an absolute path -- on Windows,
    with backslashes -- while `risk_paths` in .claude-workflow.json is written the way CI
    matches `git diff` names: relative to the checkout, forward slashes. Normalising here
    keeps one set of globs authoritative for both.

    The checkout that matters is the one holding the file, not the one the session happens
    to be sitting in. This repository is worked through several linked worktrees, so
    `.github/workflows/ci-pr.yml` in a sibling worktree is still this repository's CI
    definition and still needs risk:ci; judged only against the current work tree it was
    dropped, and a dropped path is an allowed path -- Issue #52's own failure, relocated.
    Two checkouts are the same repository when they report the same git common directory,
    compared by file identity rather than by string, because junctions, 8.3 names and UNC
    spellings all defeat a prefix match.

    None means the path belongs to no checkout of this repository -- a scratchpad, a plan
    file, an unrelated project -- and so cannot be a risk path. Callers record that.
    """
    candidate = Path(plain_spelling(value))
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
        base = root.resolve()
    except OSError:
        return None
    relative = contained_path(base, candidate)
    if relative is not None:
        return relative
    here = checkout_of(candidate)
    if not here:
        return None
    ours = checkout_of(base)
    try:
        if not ours or not os.path.samefile(here[1], ours[1]):
            return None
    except OSError:
        return None
    return contained_path(here[0], candidate)


def record_drop(root: Path, tool: str, key: str, value: str) -> None:
    """Record a target this guard could not place in any checkout of this repository.

    An unplaced path is never matched against the risk globs, which is the same outcome as
    allowing it. That is right for a scratchpad and wrong for a spelling of a repository
    file this code failed to recognise, and the two are indistinguishable from the outside.
    Issue #52 was that mistake made silently; a line here is what makes the next one
    findable rather than invisible.
    """
    log_event(root, "PolicyPathDropped", {"tool": tool, "key": key, "path": compact(value, 200)})


def edited_paths(root: Path, tool: str, tool_input: dict) -> list[str]:
    """Every repository path a write tool is about to change.

    The Edit and Write tools name their target in `file_path`, NotebookEdit in
    `notebook_path`; a patch-style command names its files in `*** Update File:` headers.
    Before Issue #52 only the header form was read, so an ordinary Edit of a risk path
    reached the risk match with an empty list and the check returned None without ever
    consulting the Issue -- fail-open on exactly the path a session uses most.
    """
    if tool not in WRITE_TOOLS:
        return []
    paths = patch_paths(str(tool_input.get("command") or ""))
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if not value:
            continue
        relative = repo_relative(root, str(value))
        if relative:
            paths.append(relative)
        else:
            record_drop(root, tool, key, str(value))
    return paths


def scraped_token(token: str) -> str:
    """One token from a command line, in the spelling the risk globs are written in.

    Three differences decide whether a glob sees the same file the shell does, and each of
    them is the whole difference between refused and allowed. Quotes are how a path with a
    space is written; backslashes are how Windows writes any path, and fnmatch only folds
    the separators on Windows, so an unfolded token is refused on the machine that ran it
    and allowed by the same check in Linux CI; and `./` is how this repository spells its
    own commands -- `ci/**` does not match `./ci/run`, which is exactly the invocation the
    project's own instructions use. Every one of the three can only add matches, since no
    risk glob is written with a quote, a backslash or a leading `./`.
    """
    plain = token.strip("'\"").replace("\\", "/")
    while plain.startswith("./"):
        plain = plain[2:]
    return plain


def write_targets(command: str) -> list[str]:
    """The tokens this command's own write verbs are aimed at.

    Every token up to the end of the statement, not just the first, because a cmdlet takes
    its path as a named parameter in any position: `-Encoding utf8 -Path ci/run` puts a
    switch value where the path would otherwise be. Tokens beginning with `-` are switches;
    the rest are candidate paths, and a candidate that matches no risk glob costs nothing.
    """
    targets: list[str] = []
    for match in WRITE_VERBS.finditer(command):
        rest = command[match.end() :]
        end = STATEMENT_END.search(rest)
        for token in rest[: end.start() if end else len(rest)].split():
            if not token.startswith("-"):
                targets.append(scraped_token(token))
    return targets


def command_paths(command: str) -> list[str]:
    """Every repository path a shell command names, spelled the way the risk globs are."""
    hinted = re.findall(RISK_PATH_HINTS, command, re.I)
    return [scraped_token(token) for token in hinted] + write_targets(command)


def risk_evidence(matches: dict[str, tuple[str, str]], labels: list[str]) -> str:
    return ", ".join(f"{label} ({matches[label][0]} matched glob {matches[label][1]})" for label in labels)


def missing_risk_labels(
    root: Path, cfg: dict, tool: str, tool_input: dict, issue_no: int | None
) -> str | None:
    command = str(tool_input.get("command") or "")
    paths = edited_paths(root, tool, tool_input)
    if tool in COMMAND_TOOLS:
        paths += command_paths(command)
    matches = risk_matches(cfg, paths)
    if not matches or not issue_no:
        return None
    # allow_stale=False: an expired copy of the Issue is not an answer to a question about
    # permission. Without it `gh` failing for any reason -- absent, unauthenticated,
    # offline, rate-limited -- serves the last labels it ever saw, so a label removed from
    # the Issue keeps satisfying this check for as long as the cache file survives.
    issue = gh_issue(root, issue_no, allow_stale=False)
    if issue is None:
        # Distinct from the missing-label refusal on purpose. Both fail closed, but the
        # fix is different, and telling someone to add a label that is already on the
        # Issue sends them looking in the one place the problem is not.
        return (
            f"Issue #{issue_no} could not be read, so the risk label(s) it needs cannot be "
            f"confirmed and this edit is refused: {risk_evidence(matches, sorted(matches))}. "
            "`gh` is missing, unauthenticated, offline or rate-limited, or its cached copy of "
            f"the Issue has expired. Run `gh issue view {issue_no}` to see which, then retry."
        )
    missing = sorted(set(matches) - label_names(issue))
    if not missing:
        return None
    return (
        "Risk-sensitive paths require Issue label(s) before editing: "
        + risk_evidence(matches, missing)
        + f". Add them to Issue #{issue_no}; the Issue is re-read after about 45 seconds."
    )


def capture_replacement(root: Path, cfg: dict, tool: str, command: str) -> str | None:
    """Reroute verbose commands through the capture wrapper to bound token cost.

    Bash alone, unlike every other check here. The wrapper re-runs what it is handed in the
    bash the gates run in, and the replacement is quoted with shlex, so feeding it a
    PowerShell command line would run different text in a different shell. This is a
    token-cost optimisation rather than a control: leaving the PowerShell tool out of it
    costs context, never enforcement.
    """
    if tool != "Bash" or not cfg.get("token_control", {}).get("capture_noisy_commands", True):
        return None
    if not NOISY.search(command) or "capture.py" in command or len(command) >= 16000:
        return None
    encoded = base64.urlsafe_b64encode(command.encode()).decode()
    wrapper = root / ".claude" / "bin" / "capture.py"
    return f'{shlex.quote(sys.executable)} "{wrapper}" --encoded {encoded}'


def main() -> int:
    event = read_event()
    root = git_root(event.get("cwd"))
    if not root:
        return 0
    cfg = config(root)
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    issue_no = current_issue(root)
    session_id = str(event.get("session_id") or "unknown")

    checks = [
        ("foreign-lease", lease_conflict(root, cfg, tool, command, issue_no, session_id)),
        ("command-policy", command_violation(root, command) if tool in COMMAND_TOOLS else None),
        ("missing-risk-label", missing_risk_labels(root, cfg, tool, tool_input, issue_no)),
    ]
    for category, reason in checks:
        if reason:
            deny(root, event, issue_no, reason, category)
            return 0

    replacement = capture_replacement(root, cfg, tool, command)
    if replacement:
        emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {"command": replacement},
                    "additionalContext": "Verbose command output is being captured to .git/claude/logs; use the bounded summary and open the full log only for the relevant failure.",
                }
            }
        )
        return 0

    log_event(
        root,
        "PreToolUse",
        {
            "session_id": session_id,
            "issue": issue_no,
            "tool": tool,
            "command_hash": hashlib.sha256(command.encode()).hexdigest()[:16] if command else None,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
