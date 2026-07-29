#!/usr/bin/env python3
from common import *


def main() -> int:
    event = read_event()
    prompt = str(event.get("prompt") or "")
    root = git_root(event.get("cwd"))
    issue_no = current_issue(root) if root else None
    secrets = scan_secrets(prompt)
    if secrets:
        if root:
            log_event(
                root,
                "PolicyDecision",
                {
                    "session_id": event.get("session_id"),
                    "issue": issue_no,
                    "decision": "block",
                    "category": "prompt-dlp",
                    "secret_types": secrets,
                },
            )
        emit(
            {
                "decision": "block",
                "reason": "Prompt appears to contain sensitive material ("
                + ", ".join(secrets)
                + "). Remove or replace it with a redacted reference before submitting.",
            }
        )
        return 0
    if not root:
        return 0
    cfg = config(root)
    if (
        cfg.get("tracking", {}).get("require_issue_for_mutation_prompts", True)
        and issue_no is None
        and mutation_prompt(prompt)
    ):
        log_event(
            root,
            "PolicyDecision",
            {
                "session_id": event.get("session_id"),
                "issue": None,
                "decision": "block",
                "category": "missing-issue",
            },
        )
        emit(
            {
                "decision": "block",
                "reason": "This prompt requests durable repository changes, but the branch has no controlling Issue number. Create/select the Issue and use a work/<issue>-<slug> branch first.",
            }
        )
        return 0
    context = (
        "Use the smallest verifiable slice. Read only files relevant to the current acceptance criterion; "
        "send verbose command output through the repository capture wrapper; update Issue state only when "
        "facts, risks, or next actions change."
    )
    if len(prompt) > int(cfg.get("token_control", {}).get("large_prompt_chars", 12000)):
        context += " The prompt is unusually large: extract requirements once into a concise task plan and avoid quoting the full prompt again."
    log_event(
        root,
        "UserPromptSubmit",
        {"session_id": event.get("session_id"), "issue": issue_no, "prompt_chars": len(prompt)},
    )
    emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
