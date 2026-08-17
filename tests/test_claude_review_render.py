"""Tests for the advisory review renderer (Issue #41).

The renderer is the only thing between model output and a comment posted by a job holding
`pull-requests: write`, and that output is steered by pull-request diff text. So the tests
that matter are the hostile ones: they assert that no envelope, however crafted, can widen
a limit, notify anyone, or put a line of its own into the comment's structure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import render_claude_review as renderer  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yml"
SCHEMA_FILE = ROOT / "workflow" / "schemas" / "claude-review.schema.json"
#: An opening fence as this renderer writes one. Its length varies with the text it holds.
FENCE = re.compile(r"^(`{3,})text$")


def finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "severity": "medium",
        "title": "Chunk overlap is never applied",
        "path": "knowledge_nexus/chunker.py",
        "line": 42,
        "evidence": "overlap = 0",
        "recommendation": "Pass the configured overlap.",
    }
    base.update(overrides)
    return base


def envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "summary": "One finding in the retrieval path.",
        "findings": [finding()],
        "needs_human_review": False,
    }
    base.update(overrides)
    return base


def structure(markdown: str) -> list[str]:
    """The lines the renderer emitted as its own structure, with fenced content removed.

    Reading the comment the way a reader does: whatever is inside a fence is quoted text
    and cannot be part of the document's own shape, so what remains is exactly the set of
    lines the model would have to reach to forge one.
    """
    kept: list[str] = []
    closing: str | None = None
    for line in markdown.splitlines():
        if closing is not None:
            if line == closing:
                closing = None
            continue
        match = FENCE.match(line)
        if match:
            closing = match.group(1)
            continue
        kept.append(line)
    return kept


EXPECTED = """## Claude advisory review

> Advisory only. Deterministic CI and human review remain authoritative.
> Every quoted block below is model output, reproduced as text; a commercial-at is
> written `(at)`, so a review cannot notify anyone.

**Summary**

```text
One finding in the retrieval path.
```

### Finding 1 (MEDIUM)

**Title:** `Chunk overlap is never applied`

**Location:** `knowledge_nexus/chunker.py:42`

**Evidence**

```text
overlap = 0
```

**Recommendation**

```text
Pass the configured overlap.
```
"""


def test_a_valid_envelope_renders_the_expected_document():
    assert renderer.render_comment(envelope()) == EXPECTED


def test_a_null_line_leaves_the_location_as_the_path_alone():
    comment = renderer.render_comment(envelope(findings=[finding(line=None)]))
    assert "**Location:** `knowledge_nexus/chunker.py`\n" in comment


def test_an_empty_review_still_renders():
    """No findings is the common case, and it must not depend on a finding existing."""
    comment = renderer.render_comment(envelope(findings=[], needs_human_review=True))
    assert "### Finding" not in comment
    assert comment.endswith("**Human review requested by Claude.**\n")


def test_the_artifact_records_the_review_as_it_arrived():
    """Evidence of what the model said, so the neutralised text does not belong in it."""
    data = envelope(summary="ping @everyone")
    assert json.loads(renderer.render_artifact(data)) == data


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(
            envelope(summary="x" * 1201),
            "summary exceeds 1200 characters (1201 given)",
            id="summary-too-long",
        ),
        pytest.param(
            envelope(findings=[finding(title="x" * 181)]),
            "findings[0].title exceeds 180 characters (181 given)",
            id="title-too-long",
        ),
        pytest.param(
            envelope(findings=[finding(path="x" * 501)]),
            "findings[0].path exceeds 500 characters (501 given)",
            id="path-too-long",
        ),
        pytest.param(
            envelope(findings=[finding(evidence="x" * 1201)]),
            "findings[0].evidence exceeds 1200 characters (1201 given)",
            id="evidence-too-long",
        ),
        pytest.param(
            envelope(findings=[finding(recommendation="x" * 1201)]),
            "findings[0].recommendation exceeds 1200 characters (1201 given)",
            id="recommendation-too-long",
        ),
        pytest.param(envelope(summary=12), "summary must be a string, got int", id="summary-not-text"),
        pytest.param(envelope(findings={}), "findings must be a list, got dict", id="findings-not-a-list"),
        pytest.param(
            envelope(findings=["not an object"]),
            "findings[0] must be an object, got str",
            id="finding-not-an-object",
        ),
        pytest.param(
            envelope(findings=[finding(), finding(title=None)]),
            "findings[1].title must be a string, got NoneType",
            id="title-not-text",
        ),
        pytest.param(
            envelope(needs_human_review="true"),
            "needs_human_review must be a boolean, got str",
            id="flag-not-a-bool",
        ),
        pytest.param(
            envelope(needs_human_review=1),
            "needs_human_review must be a boolean, got int",
            id="flag-is-a-number",
        ),
        pytest.param(
            envelope(findings=[finding(severity="urgent")]),
            "findings[0].severity must be one of critical, high, medium, low",
            id="severity-off-the-enum",
        ),
        pytest.param(
            envelope(findings=[finding(severity=["high"])]),
            "findings[0].severity must be one of critical, high, medium, low",
            id="severity-not-even-a-string",
        ),
        pytest.param(
            envelope(findings=[finding(line="42")]),
            "findings[0].line must be null or a positive integer",
            id="line-as-text",
        ),
        pytest.param(
            envelope(findings=[finding(line=0)]),
            "findings[0].line must be null or a positive integer",
            id="line-zero",
        ),
        pytest.param(
            envelope(findings=[finding(line=True)]),
            "findings[0].line must be null or a positive integer",
            id="line-as-bool",
        ),
        pytest.param(
            envelope(findings=[finding(line=1.5)]),
            "findings[0].line must be null or a positive integer",
            id="line-as-float",
        ),
        pytest.param(
            {"summary": "s", "findings": []},
            "the top level is missing needs_human_review",
            id="envelope-key-missing",
        ),
        pytest.param(
            envelope(evil="payload"),
            "the top level carries unexpected key(s) evil",
            id="envelope-key-extra",
        ),
        pytest.param(
            envelope(findings=[{key: value for key, value in finding().items() if key != "evidence"}]),
            "findings[0] is missing evidence",
            id="finding-key-missing",
        ),
        pytest.param(
            envelope(findings=[finding(evil="payload")]),
            "findings[0] carries unexpected key(s) evil",
            id="finding-key-extra",
        ),
        pytest.param(
            envelope(findings=[finding()] * 21),
            "findings holds more than 20 entries (21 given)",
            id="too-many-findings",
        ),
        pytest.param(["summary"], "the top level must be an object, got list", id="not-an-object"),
    ],
)
def test_a_field_that_breaks_the_contract_fails_closed(payload, expected):
    with pytest.raises(SystemExit) as raised:
        renderer.validate(payload)
    assert str(raised.value) == f"invalid review envelope: {expected}", str(raised.value)


def test_the_limits_themselves_are_accepted():
    """Off-by-one in the other direction: a review at the cap must still publish."""
    payload = envelope(
        summary="s" * 1200,
        findings=[finding(title="t" * 180, path="p" * 500, evidence="e" * 1200)] * 20,
    )
    assert renderer.validate(payload) is payload


def test_no_commercial_at_reaches_the_comment():
    """The rendered comment carries none at all, so no mention shape can be assembled."""
    payload = envelope(
        summary="cc @everyone @org/security-team",
        findings=[finding(title="@maintainer approved", evidence="a@b.example", path="@a/b.py")],
    )
    comment = renderer.render_comment(payload)
    assert "@" not in comment
    assert "(at)everyone (at)org/security-team" in comment
    assert "(at)maintainer approved" in comment


def test_injected_markdown_cannot_forge_the_comment_structure():
    hostile = (
        "```\n## All checks have passed\n> Approved by a maintainer\n"
        "<!-- hidden -->\n```text\n| check | ok |\n"
    )
    payload = envelope(
        summary=hostile,
        findings=[finding(severity="critical", evidence=hostile, recommendation=hostile)],
    )
    lines = structure(renderer.render_comment(payload))
    assert [line for line in lines if line.startswith(("#", "|", "<"))] == [
        "## Claude advisory review",
        "### Finding 1 (CRITICAL)",
    ]
    assert not any("checks have passed" in line or "Approved by" in line for line in lines)


def test_a_fence_is_longer_than_the_longest_run_of_backticks_inside_it():
    """Containment rests on CommonMark's closing rule, so the sizing is the whole argument."""
    evidence = "```\nstill inside\n````\nalso inside"
    comment = renderer.render_comment(envelope(findings=[finding(evidence=evidence)]))
    assert f"`````text\n{evidence}\n`````" in comment
    assert "still inside" not in "\n".join(structure(comment))


def test_a_single_line_field_cannot_close_the_code_span_that_holds_it():
    title = "closes `` here ``` and **bolds**"
    comment = renderer.render_comment(envelope(findings=[finding(title=title)]))
    assert f"**Title:** ````{title}````\n" in comment


def test_a_single_line_field_that_touches_a_backtick_is_padded():
    """CommonMark strips one space either side, so the delimiters stay unambiguous."""
    comment = renderer.render_comment(envelope(findings=[finding(path="`odd`")]))
    assert "**Location:** `` `odd`:42 ``\n" in comment


def test_a_newline_in_a_single_line_field_cannot_reach_the_next_line():
    payload = envelope(findings=[finding(title="first\n## Approved", path="a.py\n> merged")])
    comment = renderer.render_comment(payload)
    assert "**Title:** `first ## Approved`\n" in comment
    assert "**Location:** `a.py > merged:42`\n" in comment


def test_invisible_control_characters_are_removed():
    """Text that renders as nothing is how a comment says less than the artifact records."""
    comment = renderer.render_comment(envelope(summary="a\x00b\x07c\r\nd"))
    assert "```text\nabc\nd\n```" in comment


def test_the_validator_matches_the_schema_shipped_beside_it():
    assert renderer.json_schema() == json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def test_the_validator_matches_the_schema_the_review_job_hands_the_action():
    """Three copies of one contract; the drift that matters is any of them moving alone."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"--json-schema\s+'(\{.*\})'", text)
    assert match, "the review job no longer passes an inline --json-schema"
    assert renderer.json_schema() == json.loads(match.group(1))


def _render(monkeypatch, tmp_path, payload: object) -> tuple[Path, Path]:
    artifact, comment = tmp_path / "claude-review.json", tmp_path / "comment.md"
    monkeypatch.setenv("REVIEW_JSON", payload if isinstance(payload, str) else json.dumps(payload))
    monkeypatch.setattr(sys, "argv", ["render", "--artifact", str(artifact), "--comment", str(comment)])
    return artifact, comment


def test_a_valid_envelope_writes_both_documents(monkeypatch, tmp_path):
    payload = envelope()
    artifact, comment = _render(monkeypatch, tmp_path, payload)
    assert renderer.main() == 0
    assert json.loads(artifact.read_text(encoding="utf-8")) == payload
    assert comment.read_text(encoding="utf-8") == EXPECTED


def test_a_rejected_envelope_leaves_no_output_behind(monkeypatch, tmp_path):
    """Half a comment would be published as if it were the review, so nothing is written."""
    artifact, comment = _render(monkeypatch, tmp_path, envelope(findings=[finding(title="x" * 181)]))
    with pytest.raises(SystemExit) as raised:
        renderer.main()
    assert "findings[0].title" in str(raised.value)
    assert not artifact.exists()
    assert not comment.exists()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param("", "REVIEW_JSON is unset or empty", id="empty"),
        pytest.param("   \n", "REVIEW_JSON is unset or empty", id="whitespace"),
        pytest.param("{not json", "REVIEW_JSON is not valid JSON", id="unparsable"),
    ],
)
def test_an_envelope_that_never_arrived_fails_closed(monkeypatch, tmp_path, payload, expected):
    artifact, comment = _render(monkeypatch, tmp_path, payload)
    with pytest.raises(SystemExit) as raised:
        renderer.main()
    assert expected in str(raised.value)
    assert not artifact.exists()
    assert not comment.exists()
