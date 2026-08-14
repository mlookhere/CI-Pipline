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
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
