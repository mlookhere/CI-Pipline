#!/usr/bin/env python3
from __future__ import annotations

import base64
import fnmatch
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
    (r"\bdocker\s+system\s+prune\b", "Docker system pruning is prohibited in an agent session."),
    (
        r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sh|bash|zsh)\b",
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


def required_risks(cfg: dict, paths: list[str]) -> set[str]:
    result = set()
    for label, patterns in cfg.get("github", {}).get("risk_paths", {}).items():
        if any(any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in paths):
            result.add(label)
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


WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
RISK_PATH_HINTS = (
    r"(?:^|\s)(\.github/(?:workflows|actions)/\S+|[^\s]*(?:migration|schema|auth|security|deploy)[^\s]*)"
)


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


def missing_risk_labels(root: Path, cfg: dict, tool: str, command: str, issue_no: int | None) -> str | None:
    paths = patch_paths(command) if tool in WRITE_TOOLS else []
    if tool == "Bash":
        paths += re.findall(RISK_PATH_HINTS, command, re.I)
    risks = required_risks(cfg, paths)
    if not risks or not issue_no:
        return None
    missing = sorted(risks - label_names(gh_issue(root, issue_no)))
    if not missing:
        return None
    return "Risk-sensitive paths require Issue label(s) before editing: " + ", ".join(missing)


def capture_replacement(root: Path, cfg: dict, tool: str, command: str) -> str | None:
    """Reroute verbose commands through the capture wrapper to bound token cost."""
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
        ("command-policy", command_violation(root, command) if tool == "Bash" else None),
        ("missing-risk-label", missing_risk_labels(root, cfg, tool, command, issue_no)),
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
