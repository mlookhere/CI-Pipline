#!/usr/bin/env python3
"""Render the advisory review comment from output this job did not produce.

The `publish` job holds `pull-requests: write` and comments as the repository. What it
renders is model output steered by pull-request diff text, which is untrusted input, and
the `--json-schema` that constrains that output is enforced by the action, in the other
job, under no write permission. So the publisher was interpolating a shape it had never
checked: a crafted diff could push the review into emitting a finding whose text carried
mass `@`-mentions or markdown posing as a CI status or a maintainer's approval (Issue #41).

**Validated here, independently of the action.** Exact key sets at both levels, a type and
a length cap per field, `severity` restricted to the enum, `line` null or a positive
integer, `needs_human_review` a real bool. Nothing is written until the whole envelope has
passed, so a hostile payload cannot leave a half-rendered comment or a partial artifact
behind for the next step to publish. `json_schema()` re-expresses these constants as the
schema the action is handed, so the two copies that live outside this file cannot drift
away from what the publisher actually enforces.

**Contained, not filtered.** Every model-authored string is placed inside a code fence, or
a code span where it has to share a line, whose delimiter is one backtick longer than the
longest backtick run in the text. CommonMark ends a fenced block or a code span only on a
run at least as long as the one that opened it, so the text provably has no closing
sequence available anywhere inside it -- the same "an envelope its own text cannot close"
argument as `knowledge_nexus/excerpts.py`, resting on the renderer's own rule rather than
on a nonce. The alternative, a blocklist of forbidden markdown, is unbounded by
construction: there is no enumeration of the ways to draw a green tick or an approval, and
every shape missing from such a list is a live spoof. Headings and labels are emitted from
constants and from the validated `severity` alone, so the comment's own structure is never
built from model text.

`@` is the one thing containment does not settle. GitHub does not raise a mention inside
code, but the notification *is* the payload here, so it is removed rather than argued
about: every `@` becomes `(at)`, and the rendered comment therefore contains no `@` at all.
Visible and unambiguous by choice -- a zero-width separator would leave text that still
reads as a mention to a human while GitHub reads it as something else, which is a worse
thing to publish than `(at)`. The cost is that a decorator quoted in evidence reads
`(at)property`; a mention and a decorator are the same characters, so there is nothing to
tell apart and spare.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, NoReturn

ENVELOPE_KEYS = ("summary", "findings", "needs_human_review")
FINDING_KEYS = ("severity", "title", "path", "line", "evidence", "recommendation")
SEVERITIES = ("critical", "high", "medium", "low")
MAX_FINDINGS = 20
MAX_CHARS = {"summary": 1200, "title": 180, "path": 500, "evidence": 1200, "recommendation": 1200}
BACKTICKS = re.compile(r"`+")
#: C0 controls other than tab and newline. They occupy no width, so they are a way to make
#: the published comment read differently from the text recorded in the artifact.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
NOTICE = (
    "> Advisory only. Deterministic CI and human review remain authoritative.\n"
    "> Every quoted block below is model output, reproduced as text; a commercial-at is\n"
    "> written `(at)`, so a review cannot notify anyone."
)


def reject(field: str, problem: str) -> NoReturn:
    """Fail the job on one line naming the field, before anything has been written.

    The publisher has no way to render half an envelope usefully: the next step posts
    whatever file it finds, so partial success here would be published as if it were the
    review. Every rejection therefore ends the process.
    """
    raise SystemExit(f"invalid review envelope: {field} {problem}")


def require_object(value: Any, keys: tuple[str, ...], where: str) -> dict[str, Any]:
    """An object carrying exactly `keys` -- no more, no fewer.

    An extra key is rejected rather than ignored because it means the output no longer
    matches the schema the review job asked for, and this validator is the only thing left
    that would notice.
    """
    if not isinstance(value, dict):
        reject(where, f"must be an object, got {type(value).__name__}")
    missing = [key for key in keys if key not in value]
    if missing:
        reject(where, f"is missing {', '.join(missing)}")
    unexpected = sorted(set(value) - set(keys))
    if unexpected:
        reject(where, f"carries unexpected key(s) {', '.join(unexpected)}")
    return value


def require_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        reject(field, f"must be a string, got {type(value).__name__}")
    if len(value) > limit:
        reject(field, f"exceeds {limit} characters ({len(value)} given)")
    return value


def require_line(value: Any, field: str) -> None:
    if value is None:
        return
    # `bool` subclasses `int`, so `True` would otherwise be accepted and rendered as line 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        reject(field, "must be null or a positive integer")


def validate_finding(value: Any, where: str) -> None:
    finding = require_object(value, FINDING_KEYS, where)
    if finding["severity"] not in SEVERITIES:
        reject(f"{where}.severity", f"must be one of {', '.join(SEVERITIES)}")
    for field in ("title", "path", "evidence", "recommendation"):
        require_text(finding[field], f"{where}.{field}", MAX_CHARS[field])
    require_line(finding["line"], f"{where}.line")


def validate(value: Any) -> dict[str, Any]:
    """The whole envelope, or `SystemExit`. Returns the same object it was given."""
    data = require_object(value, ENVELOPE_KEYS, "the top level")
    require_text(data["summary"], "summary", MAX_CHARS["summary"])
    if not isinstance(data["needs_human_review"], bool):
        reject("needs_human_review", f"must be a boolean, got {type(data['needs_human_review']).__name__}")
    findings = data["findings"]
    if not isinstance(findings, list):
        reject("findings", f"must be a list, got {type(findings).__name__}")
    if len(findings) > MAX_FINDINGS:
        reject("findings", f"holds more than {MAX_FINDINGS} entries ({len(findings)} given)")
    for index, finding in enumerate(findings):
        validate_finding(finding, f"findings[{index}]")
    return data


def json_schema() -> dict[str, Any]:
    """The constraints above, written as the schema the `review` job hands the action.

    That schema exists twice outside this file -- inline in the workflow and in
    `workflow/schemas/claude-review.schema.json` -- and neither copy is what the publisher
    enforces. Generating the same document from the constants this validator uses lets a
    test prove all three agree, so a limit loosened in one place cannot leave the others
    quietly stricter, or the reverse.
    """
    finding = {
        "type": "object",
        "properties": {
            "severity": {"enum": list(SEVERITIES)},
            "title": {"type": "string", "maxLength": MAX_CHARS["title"]},
            "path": {"type": "string", "maxLength": MAX_CHARS["path"]},
            "line": {"type": ["integer", "null"], "minimum": 1},
            "evidence": {"type": "string", "maxLength": MAX_CHARS["evidence"]},
            "recommendation": {"type": "string", "maxLength": MAX_CHARS["recommendation"]},
        },
        "required": list(FINDING_KEYS),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": MAX_CHARS["summary"]},
            "findings": {"type": "array", "maxItems": MAX_FINDINGS, "items": finding},
            "needs_human_review": {"type": "boolean"},
        },
        "required": list(ENVELOPE_KEYS),
        "additionalProperties": False,
    }


def plain(text: str, one_line: bool = False) -> str:
    """Model text with the two things containment does not settle taken out of it."""
    text = CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    if one_line:
        text = text.replace("\n", " ").replace("\t", " ")
    return text.replace("@", "(at)")


def longest_run(text: str) -> int:
    return max((len(match.group(0)) for match in BACKTICKS.finditer(text)), default=0)


def fenced(text: str) -> str:
    """A fenced block the text inside it cannot close.

    A fence one backtick longer than the longest run in the text leaves no line inside
    able to act as the closing fence, so containment follows from CommonMark's own rule
    rather than from anything being inspected or forbidden.
    """
    marker = "`" * max(3, longest_run(text) + 1)
    return f"{marker}text\n{text}\n{marker}"


def spanned(text: str) -> str:
    """The same rule for a single line: a code span sized past the text's own backticks.

    A span whose content touches a backtick, or is empty, is padded with the spaces
    CommonMark strips back off, so the delimiters stay distinguishable from the content.
    """
    marker = "`" * (longest_run(text) + 1)
    padded = text if text and not text.startswith("`") and not text.endswith("`") else f" {text} "
    return f"{marker}{padded}{marker}"


def render_finding(index: int, finding: dict[str, Any]) -> list[str]:
    """One finding. The heading carries the position and the validated severity only.

    Nothing model-authored is placed in a heading: headings are how a reader tells this
    comment's own structure from its contents, and a contained string in one would still
    be the review deciding what the structure looks like.
    """
    where = plain(finding["path"], one_line=True)
    if finding["line"] is not None:
        where = f"{where}:{finding['line']}"
    return [
        "",
        f"### Finding {index} ({finding['severity'].upper()})",
        "",
        f"**Title:** {spanned(plain(finding['title'], one_line=True))}",
        "",
        f"**Location:** {spanned(where)}",
        "",
        "**Evidence**",
        "",
        fenced(plain(finding["evidence"])),
        "",
        "**Recommendation**",
        "",
        fenced(plain(finding["recommendation"])),
    ]


def render_comment(data: dict[str, Any]) -> str:
    lines = [
        "## Claude advisory review",
        "",
        NOTICE,
        "",
        "**Summary**",
        "",
        fenced(plain(data["summary"])),
    ]
    for index, finding in enumerate(data["findings"], start=1):
        lines.extend(render_finding(index, finding))
    if data["needs_human_review"]:
        lines.extend(["", "**Human review requested by Claude.**"])
    return "\n".join(lines) + "\n"


def render_artifact(data: dict[str, Any]) -> str:
    """The validated envelope as it arrived.

    Deliberately not the neutralised text: the artifact is the record of what the review
    actually said, and it is JSON in a downloadable archive rather than markdown anyone
    renders, so the reasons for rewriting the comment do not apply to it.
    """
    return json.dumps(data, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and render the advisory review comment.")
    parser.add_argument("--artifact", required=True, help="path to write the validated envelope to")
    parser.add_argument("--comment", required=True, help="path to write the comment body to")
    args = parser.parse_args()
    # The envelope arrives in the environment rather than on the command line, so that no
    # part of it is ever expanded by the shell that starts this process.
    raw = os.environ.get("REVIEW_JSON", "")
    if not raw.strip():
        reject("REVIEW_JSON", "is unset or empty")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        reject("REVIEW_JSON", f"is not valid JSON: {exc}")
    envelope = validate(data)
    # Both documents are built before either is written, so a rejection cannot leave one
    # of them on disk for the publish step to find.
    artifact, comment = render_artifact(envelope), render_comment(envelope)
    Path(args.artifact).write_text(artifact, encoding="utf-8")
    Path(args.comment).write_text(comment, encoding="utf-8")
    print(f"rendered {len(envelope['findings'])} finding(s) to {args.comment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
