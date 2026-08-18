#!/usr/bin/env python3
from __future__ import annotations

import base64
import fnmatch
import os
import re
import shlex
import sys

from common import *

# `git`, `git.exe`, a path to either, and any run of global options in between -- including
# `-c key=value` and `-C path`, which take a separate word. Written once and shared by every
# rule that names git, because `git.exe commit --no-verify` and `git -C . push origin dev`
# defeated all of them: each pattern spelled the invocation as two literal words (Issue #90).
GIT = r"(?:[^\s;|&]*[/\\])?git(?:\.exe)?(?:\s+-[cC]\s+\S+|\s+-\S+)*\s+"
GIT_NAMES = {"git", "git.exe"}
# Global options that consume the following word, so the subcommand is not simply the next
# one along.
GIT_GLOBALS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
# Push options that consume the following word, so their value is not a refspec. Not eating
# them is what let `git push -o ci.skip origin` read `origin` as a refspec: the push then
# looked like it named a branch, and the bare-push rule that would have refused it was
# skipped (Issue #90).
PUSH_OPTIONS_WITH_VALUE = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
# These write every branch the remote has, protected ones included, while naming none of
# them. No refspec appears, so every rule that reads refspecs is blind to them.
WRITES_EVERY_BRANCH = {"--mirror", "--all"}
# `-f` survives bundling: `-fu` is a force push and matched neither the `-f(?:\s|$)` in DENY
# nor the leading `+` this hook reads.
BUNDLED_FORCE = re.compile(r"^-[A-Za-z]*f[A-Za-z]*$")

DENY = [
    (rf"\b{GIT}(?:commit|push)\b[^\n]*--no-verify\b", "Hook bypasses are prohibited; fix the failing hook."),
    (
        rf"\b{GIT}push\b[^\n]*(?:--force(?:-with-lease|-if-includes)?(?:=\S+)?(?:\s|$)|-f(?:\s|$))",
        "Force-pushing is prohibited by the standard workflow.",
    ),
    (r"\bgh\s+pr\s+merge\b[^\n]*--admin\b", "Administrator merge bypasses are prohibited."),
    (rf"\b{GIT}reset\s+--hard\b", "Hard reset is prohibited in a standard task session."),
    (rf"\b{GIT}clean\b[^\n]*(?:-[A-Za-z]*[fx][A-Za-z]*|--force)", "Destructive git clean is prohibited."),
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
        rf"\b{GIT}config\b[^\n]*(?:http\..*extraheader|credential\.helper)",
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


# A token carrying either of these is a redirection, not a ref. `2>&1` survived the switch
# filter because it starts with a digit, and reading it as a refspec is what let
# `git push 2>&1 | tail` look like a push that named a branch.
REDIRECTION = re.compile(r"[<>]")


def statements(command: str) -> list[str]:
    """`command` split where one command ends and the next begins.

    Every push has to be read, not just the first. `GIT_PUSH.search` returned one match, so
    `git push origin work/52-x && git push origin dev` was judged entirely on its harmless
    first half (Issue #90).
    """
    return STATEMENT_END.split(command)


def tokenise(statement: str) -> list[str]:
    """Words with their quotes removed.

    A whitespace split leaves them on, so `git push origin "dev"` produced the token `"dev"`,
    which is not the name of any branch and matched no protected one. `'+dev'` defeated the
    force-push rule the same way. Stripping first also means a quoted switch is still read as
    a switch (Issue #90).
    """
    return [token for token in (word.strip("\"'") for word in statement.split()) if token]


def push_arguments(statement: str) -> list[str] | None:
    """The words `git push` was given here, or None when this statement runs no push.

    The invocation is walked rather than matched as two literal words, so a path, a `.exe`
    suffix and any run of global options are all recognised. `git.exe push origin dev` and
    `git -C . push origin dev` were both invisible to a `\\bgit\\s+push\\b` pattern, and so
    were the config-driven forms like `git -c remote.origin.push=... push` (Issue #90).
    """
    tokens = tokenise(statement)
    index = 0
    while index < len(tokens):
        name = tokens[index].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in GIT_NAMES:
            break
        index += 1
    else:
        return None

    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token in GIT_GLOBALS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(tokens) or tokens[index].lower() != "push":
        return None
    return tokens[index + 1 :]


def split_push_arguments(args: list[str]) -> tuple[list[str], set[str]]:
    """(positional words, switch names), with option values consumed rather than read as refs."""
    positional: list[str] = []
    switches: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            switches.add(name)
            index += 2 if name in PUSH_OPTIONS_WITH_VALUE and "=" not in token else 1
            continue
        if REDIRECTION.search(token):
            index += 1
            continue
        positional.append(token)
        index += 1
    return positional, switches


def parsed_pushes(command: str) -> list[tuple[list[str], set[str]]]:
    """(named refs, switches) for every `git push` in `command`.

    The refs are the positional words after the remote. An empty list means the push names
    nothing and would send the current branch -- the case that has to be judged against the
    session's own branch instead.

    `HEAD` counts as naming nothing: what it resolves to cannot be read out of the command
    text, so it is judged as if no refspec were given (Issue #87).
    """
    pushes = []
    for statement in statements(command):
        args = push_arguments(statement)
        if args is None:
            continue
        positional, switches = split_push_arguments(args)
        refs = [ref for ref in positional[1:] if ref.upper() != "HEAD"]
        pushes.append((refs if len(positional) > 1 else [], switches))
    return pushes


def pushed_refs(command: str) -> list[str]:
    """The refs the first `git push` in `command` names, or [] when it names none."""
    pushes = parsed_pushes(command)
    return pushes[0][0] if pushes else []


def destination_branch(ref: str) -> str:
    """The branch name a refspec writes to, stripped of everything that hides it.

    Three spellings all name `dev` and none of them is the bare word: `+dev` (a force push),
    `HEAD:refs/heads/dev` (a fully qualified destination) and `:dev` (a deletion). Only the
    half after the last colon is written -- in `src:dst` the source is read, so a task branch
    pushed to `dev` is a push to `dev` no matter what it is called locally.
    """
    ref = ref.lstrip("+").rsplit(":", 1)[-1]
    for prefix in ("refs/heads/", "heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def writes_protected_branch(ref: str, branches: list[str]) -> bool:
    """Whether this refspec writes one of `branches`.

    Matched as a glob rather than compared, because a refspec may be one:
    `git push origin 'refs/heads/*:refs/heads/*'` writes every branch there is and equals
    none of them by name (Issue #90). fnmatchcase, not fnmatch -- git refs are
    case-sensitive and fnmatch would fold them on Windows.
    """
    destination = destination_branch(ref)
    return any(fnmatch.fnmatchcase(name, destination) for name in branches)


def protected_push(root: Path, command: str) -> str | None:
    """Why this push is refused, or None.

    Two different prohibitions, kept apart because they have different fixes and the reader
    needs to know which one they hit (Issue #87). Naming `dev` or `master` as the destination
    is refused from anywhere. Giving no refspec at all is refused only while the session's
    own branch is protected, since that -- and only that -- is when a bare `git push` pushes
    a protected branch.

    Reading `branch(root)` first was the bug: `./flow new` leaves the session on `dev` by
    design, so that test was true in every ordinary session and refused every hand push of a
    task branch, with a message naming a prohibition the command did not violate. It is the
    same mistake Issue #78 fixed for risk labels -- the wrong checkout answering -- in the
    one place #78 did not reach.

    The destination is read from the refs, not from the command text. Searching the text was
    the third defect in this guard (Issue #90): it required the protected name to sit between
    a space-or-colon and a space-or-end, so `git push origin +dev` -- a force push to `dev` --
    did not match, and `pushed_refs` returning `['+dev']` then made it too non-bare for the
    rule below to catch either. A ref is a ref; parse it once and judge the parse.

    Every push in the command is judged, not only the first.
    """
    branches = list(config(root).get("branches", {}).values()) or ["main", "master", "dev"]
    for named, switches in parsed_pushes(command):
        if switches & WRITES_EVERY_BRANCH:
            option = ", ".join(sorted(switches & WRITES_EVERY_BRANCH))
            return (
                f"{option} pushes every branch the remote has, including the integration and "
                "production branches, without naming any of them. Push the task branch by name."
            )
        if any(writes_protected_branch(ref, branches) for ref in named):
            return "Direct pushes to integration or production branches are prohibited; use a pull request."
        if not named and branch(root) in branches:
            return (
                f"This push names no branch, so it would push {branch(root)!r}, which is an "
                "integration or production branch. Push the task branch by name, or open a pull "
                "request."
            )
    return None


def audit(root: Path, event_name: str, payload: dict) -> None:
    """Record what happened, and never let recording it change what happens.

    `log_event` opens a file, so it can raise for reasons that have nothing to do with the
    decision being logged: a read-only `.git`, a full disk, a path Windows will not accept.
    Raising out of `deny` used to mean the denial was never emitted, and because
    `.claude/hooks/run` execs Python the hook then exited 1 -- a non-blocking error -- so the
    command that was about to be refused ran instead (Issue #90). Telemetry is not worth a
    bypass.
    """
    try:
        log_event(root, event_name, payload)
    except Exception:
        pass


def deny(root: Path, event: dict, issue_no: int | None, reason: str, category: str) -> None:
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
    audit(
        root,
        "PolicyDecision",
        {
            "session_id": event.get("session_id"),
            "issue": issue_no,
            "decision": "deny",
            "category": category,
        },
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
# same in each, `cp`/`mv`/`rm` are PowerShell aliases as well as POSIX commands, and a
# PowerShell session reaches for Set-Content where bash reaches for `tee`. Reads are
# deliberately absent: Get-Content of a risk path changes nothing.
#
# Issue #59 covered redirection and the content cmdlets; Issue #60 measured what was left
# and adds the verbs that were still walking through -- the in-place editors, the commands
# that put a file somewhere, `git checkout`/`git restore` writing a path back out of the
# object store, and `chmod`, because clearing the executable bit on ci/run disables the gate
# as effectively as editing it. What no verb list reaches is recorded at hinted_paths.
#
# `sed` and `perl` are verbs only when an in-place switch is present: `sed -n 1,5p ci/run`
# reads. `install` has to be the first word of its statement, since `pip install -r
# requirements.txt` would otherwise read as a write to the manifest it only reads.
#
# Not every `>` opens a file, and Issue #78 is what a naive `>>?` cost: `2>&1` points one
# descriptor at another and writes nothing, so `cat requirements.txt 2>&1 && ...` was read as
# a write to every token after it. `fddup` claims those spellings first -- alternation is
# leftmost-first, so `2>&1` is taken as a duplication before the bare `>` can see it -- and
# write_targets skips whatever it matched. The direction matters: this is the one alternative
# whose job is to *withhold* a match, so it has to be narrow. It requires a descriptor or a
# close (`2>&1`, `>&2`, `2>&-`) after the `&`; `>&file` and `&>file` are real writes to a
# file in bash and stay with the redirect alternatives that follow.
WRITE_VERBS = re.compile(
    r"(?P<fddup>\d*>&\s*(?:\d+-?|-))"
    r"|&>>?|\d*>>?"
    r"|\b(?:Set-Content|Add-Content|Clear-Content|Out-File|New-Item|Remove-Item|Move-Item"
    r"|Copy-Item|Rename-Item|Set-ItemProperty|Tee-Object|tee|cp|mv|rm|ln|chmod|truncate)\b"
    r"|\bgit\s+(?:checkout|restore)\b"
    r"|\b(?:sed|perl)\b(?=[^;|\n]*\s(?:-{1,2}[A-Za-z]*i\b|--in-place))"
    r"|(?:^|[;|&\n]\s*)install\b",
    re.I,
)
# Where a verb's reach ends. `&&` and a trailing `&` end a statement as surely as `;` does,
# and leaving them out was the other half of Issue #78: a write verb's target list ran to the
# end of the whole chain, so `echo a > b.txt && cat ci/run` called `ci/run` a write target.
# `||` was already covered by `|`, by its first character.
#
# The `&` is conditional because two of the spellings above are built from one. `tee 2>&1
# ci/run` must not stop at the `&` inside the duplication, or the file it writes falls off the
# end of the scan; and `&>` must not stop before the redirect it belongs to. Hence: an `&`
# ends a statement only when it neither follows a `>` nor precedes one. The lookbehind is why
# write_targets searches the whole command from an offset rather than a slice -- at index 0 of
# a slice there is nothing behind to look at, and the guard silently stops guarding.
STATEMENT_END = re.compile(r"[;|\n]|(?<!>)&(?!>)")


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


def forced_push(command: str) -> str | None:
    """The two force-push spellings that announce nothing.

    The DENY entry above matches `--force`, `--force-with-lease` and a lone `-f`, which is
    every spelling that looks like one. Two do not: a leading `+` on a refspec, and `-f`
    bundled with other short options as `-fu`. Both are the same operation and matched
    nothing at all (Issue #90). Read from the parsed push rather than the command text, so a
    `+` inside an unrelated later argument is not mistaken for one.
    """
    for named, switches in parsed_pushes(command):
        if any(ref.startswith("+") for ref in named):
            return "Force-pushing is prohibited by the standard workflow; '+ref' is a force push."
        if any(BUNDLED_FORCE.match(switch) for switch in switches):
            return "Force-pushing is prohibited by the standard workflow; '-f' is a force push."
    return None


def command_violation(root: Path, command: str) -> str | None:
    for pattern, reason in DENY:
        if re.search(pattern, command, re.I):
            return reason
    return forced_push(command) or protected_push(root, command)


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


def locate_path(root: Path, value: str) -> tuple[Path, str] | None:
    """The checkout holding `value`, and `value` relative to it. See repo_relative.

    Both halves are returned because both are needed and only one of them used to be. The
    relative path answers which risk globs apply; the checkout answers which Issue is allowed
    to carry the labels those globs require, and reading that from the session's checkout
    instead was Issue #78's deadlock -- `./flow new` puts the work in a linked worktree and
    leaves the session on `dev`, where no Issue exists to hold any label.
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
        return base, relative
    here = checkout_of(candidate)
    if not here:
        return None
    ours = checkout_of(base)
    try:
        if not ours or not os.path.samefile(here[1], ours[1]):
            return None
    except OSError:
        return None
    relative = contained_path(here[0], candidate)
    return None if relative is None else (here[0], relative)


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
    located = locate_path(root, value)
    return None if located is None else located[1]


def record_drop(root: Path, tool: str, key: str, value: str) -> None:
    """Record a target this guard could not place in any checkout of this repository.

    An unplaced path is never matched against the risk globs, which is the same outcome as
    allowing it. That is right for a scratchpad and wrong for a spelling of a repository
    file this code failed to recognise, and the two are indistinguishable from the outside.
    Issue #52 was that mistake made silently; a line here is what makes the next one
    findable rather than invisible.
    """
    log_event(root, "PolicyPathDropped", {"tool": tool, "key": key, "path": compact(value, 200)})


def located_paths(root: Path, tool: str, tool_input: dict) -> list[tuple[Path, str]]:
    """Every repository path a write tool is about to change, with its owning checkout.

    The Edit and Write tools name their target in `file_path`, NotebookEdit in
    `notebook_path`; a patch-style command names its files in `*** Update File:` headers.
    Before Issue #52 only the header form was read, so an ordinary Edit of a risk path
    reached the risk match with an empty list and the check returned None without ever
    consulting the Issue -- fail-open on exactly the path a session uses most.

    Header paths are attributed to the session's checkout because that is what they are
    relative to; an absolute target is attributed to whichever checkout actually holds it.
    """
    if tool not in WRITE_TOOLS:
        return []
    located = [(root, path) for path in patch_paths(str(tool_input.get("command") or ""))]
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if not value:
            continue
        found = locate_path(root, str(value))
        if found:
            located.append(found)
        else:
            record_drop(root, tool, key, str(value))
    return located


def edited_paths(root: Path, tool: str, tool_input: dict) -> list[str]:
    """The repo-relative half of located_paths, for callers that only match globs."""
    return [relative for _, relative in located_paths(root, tool, tool_input)]


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

    A descriptor duplication contributes nothing: `2>&1` is the one spelling here that looks
    like a write and is not one, so the match is consumed and discarded rather than left for
    the bare `>` to claim (Issue #78).

    The statement end is searched in the full command from the verb's offset, never in a
    slice of it. STATEMENT_END decides on the character before the `&`, and a slice starting
    at that offset has none -- which would silently turn the `tee 2>&1 ci/run` guard back off.
    """
    targets: list[str] = []
    for match in WRITE_VERBS.finditer(command):
        if match.group("fddup"):
            continue
        end = STATEMENT_END.search(command, match.end())
        for token in command[match.end() : end.start() if end else len(command)].split():
            if not token.startswith("-"):
                targets.append(scraped_token(token))
    return targets


def hinted_paths(command: str) -> list[str]:
    """The risk paths a command names without a write verb aimed at them.

    Kept apart from write_targets because the two carry different evidence. A named path is
    a reason to want the label; a path a write verb points at is a change. The Issue-less
    branch below refuses only the second, so that a session on `dev` can still read and
    grep the files it may not edit (Issue #60).

    What no scrape reaches, stated rather than implied: a shell command's write set is only
    decidable by running it. `python - <<'PY' ... open(risk_path, "w") ... PY`, `bash -c`,
    `git apply`, `patch < diff`, `dd of=`, and a package manager rewriting a manifest it was
    never asked about all change files this list cannot name. Scraping string literals out
    of an inline script was measured and rejected: it refuses honest commands
    (`python -c "print(open('.claude-workflow.json').read())"`) while one composed path
    (`open("knowledge_nexus/" + name, "w")`) walks straight through, which buys the
    appearance of a control and not a control. Those writes are caught on the diff instead,
    by check_risk_labels in workflow/validate_pr.py, which reads what changed rather than
    what was typed and is the authoritative gate on every PR.

    The same undecidability has cheaper spellings, named here so the residual risk is stated
    at its real size rather than at the size of the examples above. The target reaches the
    file but not this scraper when it is indirected through a variable (`f=ci/run; echo x > $f`),
    spelled through an expansion (`echo x > $PWD/ci/run`), quoted mid-token (`echo x > ci"/"run`,
    since only leading and trailing quotes are stripped), written with a leading `.//`
    (`./` is folded only at the start), or placed after `>|`, whose `|` reads as a statement
    end and truncates the target list. Each is a real bypass of this function and none is a
    bypass of the diff gate, which is why the diff gate is the one called authoritative.
    """
    return [scraped_token(token) for token in re.findall(RISK_PATH_HINTS, command, re.I)]


def risk_evidence(matches: dict[str, tuple[str, str]], labels: list[str]) -> str:
    return ", ".join(f"{label} ({matches[label][0]} matched glob {matches[label][1]})" for label in labels)


def uncontrolled_change(root: Path, cfg: dict, written: list[str]) -> str | None:
    """Reason to refuse a risk-path change made from a branch that carries no Issue.

    `current_issue` reads the number out of the branch name, so on `dev`, on `master` and on
    a detached HEAD there is no Issue to hold the labels -- and the guard used to return None
    there, which left the least-controlled branch as the only one with no check at all
    (Issue #60). This repository's protocol already requires a controlling Issue for durable
    mutation, so the answer is a refusal that names the branch to move to, rather than a
    question that was never asked.

    Only what the tool evidently writes counts, never every risk path the command names: a
    session sitting on `dev` legitimately reads, greps and runs the gates over ci/ and
    .claude/, and refusing that would refuse work the protocol permits. On a task branch a
    named path still needs its label, because there an Issue exists to carry one.
    """
    changes = risk_matches(cfg, written)
    if not changes:
        return None
    return (
        "Risk-sensitive paths may only be changed from a task branch that names its "
        f"controlling Issue, and branch {branch(root)!r} names none, so no Issue can carry "
        "the required label(s): "
        + risk_evidence(changes, sorted(changes))
        + ". Create or check out work/<issue>-slug for the Issue that owns this change, "
        "then retry there."
    )


CHECKOUT_ISSUE_CACHE: dict[str, int | None] = {}


def issue_of(checkout: Path, root: Path, issue_no: int | None) -> int | None:
    """The Issue controlling `checkout`, memoised because this runs before every tool call."""
    key = os.path.normcase(str(checkout))
    if key == os.path.normcase(str(root)):
        return issue_no
    if key not in CHECKOUT_ISSUE_CACHE:
        CHECKOUT_ISSUE_CACHE[key] = current_issue(checkout)
    return CHECKOUT_ISSUE_CACHE[key]


def by_checkout(pairs: list[tuple[Path, str]]) -> dict[str, tuple[Path, list[str]]]:
    grouped: dict[str, tuple[Path, list[str]]] = {}
    for checkout, relative in pairs:
        entry = grouped.setdefault(os.path.normcase(str(checkout)), (checkout, []))
        entry[1].append(relative)
    return grouped


def missing_risk_labels(
    root: Path, cfg: dict, tool: str, tool_input: dict, issue_no: int | None
) -> str | None:
    """Reason to refuse, when a risk path is changed without the labels that path requires.

    Judged once per checkout rather than once per call (Issue #78). repo_relative already
    holds that "the checkout that matters is the one holding the file, not the one the
    session happens to be sitting in", and resolves the path that way; resolving the
    controlling *Issue* from the session's branch instead contradicted it, and deadlocked the
    workflow this repository prescribes -- `./flow new` puts the work in a linked worktree
    whose branch names the Issue, and leaves the session on `dev`, which names none.

    The answer is the conjunction, never the most permissive checkout: every checkout with a
    risk match must have an Issue carrying that match's label, and the first one that does
    not is the refusal. Nothing here relaxes what a label means. It stays true that a branch
    naming no Issue cannot change a risk path, and that an Issue lacking the label cannot
    either -- and no new authority is created, because any session could already name a
    branch `work/<n>-x` in its own checkout to be judged against Issue n.
    """
    command = str(tool_input.get("command") or "")
    written = located_paths(root, tool, tool_input)
    named: list[tuple[Path, str]] = []
    if tool in COMMAND_TOOLS:
        # Command tokens are relative to where the command runs, which is the session's
        # checkout, so they are attributed there rather than resolved per token.
        written += [(root, target) for target in write_targets(command)]
        named = [(root, hint) for hint in hinted_paths(command)]
    changes = by_checkout(written)
    for key, (checkout, paths) in sorted(by_checkout(written + named).items()):
        reason = checkout_risk_refusal(
            root, cfg, checkout, paths, changes.get(key, (checkout, []))[1], issue_no
        )
        if reason:
            return reason
    return None


def checkout_risk_refusal(
    root: Path,
    cfg: dict,
    checkout: Path,
    paths: list[str],
    written: list[str],
    issue_no: int | None,
) -> str | None:
    """Reason to refuse the part of this call that lands in one checkout."""
    matches = risk_matches(cfg, paths)
    if not matches:
        return None
    controlling = issue_of(checkout, root, issue_no)
    if not controlling:
        return uncontrolled_change(checkout, cfg, written)
    # Live, never cached (Issue #60): cache_json keeps its answer in .git/claude/cache/,
    # which no risk glob and no permission rule covers, so the labels came back from a file
    # this session can write. Issue #52 stopped an *expired* copy from answering; a forged
    # one is not expired.
    issue = gh_issue_live(root, controlling)
    if issue is None:
        # Distinct from the missing-label refusal on purpose. Both fail closed, but the
        # fix is different, and telling someone to add a label that is already on the
        # Issue sends them looking in the one place the problem is not.
        return (
            f"Issue #{controlling} could not be read, so the risk label(s) it needs cannot be "
            f"confirmed and this edit is refused: {risk_evidence(matches, sorted(matches))}. "
            "`gh` is missing, unauthenticated, offline or rate-limited. Run "
            f"`gh issue view {controlling}` to see which, then retry."
        )
    missing = sorted(set(matches) - label_names(issue))
    if not missing:
        return None
    return (
        "Risk-sensitive paths require Issue label(s) before editing: "
        + risk_evidence(matches, missing)
        + f". Add them to Issue #{controlling}, then retry."
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

    audit(
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


def guarded_main() -> int:
    """Run the policy, and block rather than shrug if it cannot run.

    Nothing in `main` was wrapped, so any unexpected exception -- in a lease read, a label
    lookup, a subprocess that timed out -- left Python exiting 1. On `PreToolUse` that is a
    non-blocking error: the message is surfaced and the tool call proceeds anyway. So the one
    hook whose entire job is refusing commands stopped refusing them whenever it broke, which
    is the failure mode `.claude/hooks/run` already refuses to accept for itself (`run:24-31`
    picks 2, not 1, for exactly this hook). This makes the Python side agree with the wrapper
    (Issue #90).
    """
    try:
        return main()
    except Exception as error:
        print(
            f"pre_tool_policy failed to evaluate this command: {type(error).__name__}: {error}\n"
            "Refusing it rather than allowing an unevaluated command. "
            "Repository policy hooks are NOT enforcing until this is fixed.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(guarded_main())
