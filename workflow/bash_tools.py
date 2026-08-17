"""Resolve a bash that shares this process's environment and filesystem.

`bash` on PATH is `C:\\Windows\\System32\\bash.exe` on many Windows installs: the WSL
launcher. It exists, it is executable, and it runs -- but it runs a *different operating
system*. It does not inherit Windows environment variables (only what `WSLENV` lists) and it
cannot see the interpreters this repository exports, so a stage command arrives with an empty
`$PROJECT_PYTHON` and dies as `: command not found`.

That is the same defect as the `python3` Microsoft Store stub this repository already guards
against: a name on PATH that resolves to something which cannot do the job. The same answer
applies -- do not trust the name, run the candidate and check it behaves. Here the property
that matters is environment propagation, so that is what gets probed rather than trying to
recognise WSL by its path (Issue #35).

The module also carries `use_utf8_streams()`, which has nothing to do with resolving bash.
It lives here because this is the leaf of the workflow import graph: it depends on nothing
in this repository, so every entry point can import it without risking a cycle, and two of
the three that print captured output already import it for `bash_command()`. A module of
its own would be a fourth file holding nine lines, and a copy per entry point is precisely
how the copies drift out of step -- which is what Issue #77 was, the fix for Issue #67
never having reached the capture wrapper.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROBE_VARIABLE = "CLAUDE_BASH_PROBE"
PROBE_VALUE = "environment-reaches-bash"
_RESOLVED: str | None = None


def git_sibling_bash() -> str | None:
    """The bash shipped with Git, found through the `git` already on PATH."""
    git = shutil.which("git")
    if not git:
        return None
    # .../Git/cmd/git.exe and .../Git/bin/git.exe both sit one directory below the install.
    install = Path(git).resolve().parent.parent
    candidate = install / "bin" / "bash.exe"
    return str(candidate) if candidate.is_file() else None


def bash_candidates() -> list[str]:
    """Ordered candidates, most trustworthy first, with duplicates removed."""
    ordered = [os.environ.get("CLAUDE_BASH") or None]
    if os.name == "nt":
        ordered.append(git_sibling_bash())
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                ordered.append(str(Path(root) / "Git" / "bin" / "bash.exe"))
    ordered.append(shutil.which("bash"))
    seen: dict[str, None] = {}
    for candidate in ordered:
        if candidate:
            seen.setdefault(candidate, None)
    return list(seen)


def probe_environment() -> dict[str, str]:
    """A minimal environment for the probe.

    Deliberately not `os.environ`: the probe exists to answer one question, and handing a
    child process every credential in scope to answer it is exactly what the repository's
    rules forbid. PATH and SystemRoot are what a Windows executable needs to start at all.
    """
    environment = {PROBE_VARIABLE: PROBE_VALUE}
    for name in ("PATH", "SystemRoot", "SYSTEMROOT"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def passes_environment(candidate: str) -> bool:
    """True when a variable exported here arrives intact inside `candidate`."""
    environment = probe_environment()
    try:
        probe = subprocess.run(
            [candidate, "--noprofile", "--norc", "-c", f'printf %s "${PROBE_VARIABLE}"'],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and (probe.stdout or "").strip() == PROBE_VALUE


def bash_command() -> str:
    """The bash to run repository commands with. Probed once per process."""
    global _RESOLVED
    if _RESOLVED is None:
        for candidate in bash_candidates():
            if passes_environment(candidate):
                _RESOLVED = candidate
                break
        else:
            raise SystemExit(
                "no usable bash found. On Windows a bare `bash` is often the WSL launcher, "
                "which cannot see this process's environment; install Git for Windows or set "
                "CLAUDE_BASH to a bash that can."
            )
    return _RESOLVED


def use_utf8_streams() -> None:
    """Encode what this process prints as UTF-8 whatever codec the interpreter picked.

    `claude_flow.output()` already decodes gh as UTF-8; the loss is on the way back out.
    When stdout is a pipe Python encodes it with the locale codec -- cp1252 on Windows --
    so the arrow in control Issue #12 killed `./flow start` before it could print the
    acceptance criteria it exists to show (Issue #67). The capture wrapper had the same
    hole and a worse blast radius: every GitHub Actions job log line opens with a
    byte-order mark, and every byte the wrapper could not decode is already a U+FFFD by
    the time it is printed, neither of which cp1252 can encode. So reading a job log
    through the wrapper replaced a command's real result with a traceback raised by the
    reporting code itself (Issue #77). Every subcommand prints captured gh output, so the
    two streams are fixed once at the process boundary rather than at each print. A stream
    that is already UTF-8 (any Linux runner, a Windows console) is unchanged by this, and a
    stream that cannot be reconfigured at all -- no console, a plain StringIO -- is left
    alone. backslashreplace over the default strict so that a character UTF-8 cannot carry,
    a lone surrogate out of a filesystem path, stays legible in the output instead of
    aborting the briefing.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
