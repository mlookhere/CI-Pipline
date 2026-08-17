"""Tests for the Action pinning gate (Issue #15).

`pin_actions.py` shipped with no tests, and the one regex it is built on recognised only
25 of this repository's 40 `uses:` references. The other 15 were not reported as unpinned --
they were never seen at all, which is the failure mode that matters: `--check` printed
"All external GitHub Actions are pinned to full commit SHAs." while fifteen references sat
on floating tags. A gate that cannot see a violation reports success forever.

So the load-bearing test here is not "does the regex match this string" but
`test_every_uses_line_in_the_repository_is_recognised`, which compares the matcher against a
second, deliberately naive reader of the same files. Recognition is pinned first; pinning is
pinned second.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

# E402: `workflow/` is not an importable package, so sys.path has to be extended first.
# Suppressed for that reason alone, matching tests/test_workflow_policy.py.
import pin_actions  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"


def naive_uses_lines(text: str) -> list[int]:
    """A second reader of the same files, written without a regex.

    Independent of `PATTERN` on purpose. If the two readers shared an implementation they
    would share the blind spot as well, and the comparison below would prove nothing.
    """
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].lstrip()
        if stripped.startswith("uses:"):
            found.append(lineno)
    return found


@pytest.mark.parametrize(
    "line,repo,ref",
    [
        # `uses:` as the step's first key -- the only form the original regex accepted.
        pytest.param("      - uses: actions/checkout@v6", "actions/checkout", "v6", id="dashed"),
        # `uses:` under `- name:`, `- id:` or `- if:`, so the dash belongs to the earlier key.
        # 13 references in this repository are written this way (Issue #15).
        pytest.param(
            "        uses: actions/upload-artifact@v7", "actions/upload-artifact", "v7", id="indented"
        ),
        pytest.param(
            "        uses: anthropics/claude-code-action@v1",
            "anthropics/claude-code-action",
            "v1",
            id="indented-under-id",
        ),
        # A sub-path action: three segments, not two.
        pytest.param(
            "      - uses: github/codeql-action/init@v4", "github/codeql-action/init", "v4", id="sub-path"
        ),
        pytest.param(
            "        uses: github/codeql-action/analyze@v4",
            "github/codeql-action/analyze",
            "v4",
            id="indented-sub-path",
        ),
        # The 40-hex strings below are public `actions/checkout` commit SHAs -- the exact
        # thing this gate exists to produce. detect-secrets scores them as high-entropy hex,
        # so they are allowlisted individually rather than by excluding the file.
        pytest.param(
            "      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6",
            "actions/checkout",
            "d23441a48e516b6c34aea4fa41551a30e30af803",  # pragma: allowlist secret
            id="already-pinned",
        ),
        # A reusable-workflow call. This repository has none today, but the form is legal and
        # a `.github` path segment does not start with an alphanumeric -- so anchoring every
        # segment the way the owner is anchored would skip it exactly as silently as the
        # two-segment pattern skipped codeql-action. Only the owner carries that anchor.
        pytest.param(
            "    uses: owner/repo/.github/workflows/ci.yml@v1",
            "owner/repo/.github/workflows/ci.yml",
            "v1",
            id="reusable-workflow",
        ),
    ],
)
def test_a_reference_the_matcher_must_recognise(line, repo, ref):
    match = pin_actions.PATTERN.match(line)
    assert match is not None, f"reference not recognised, so it can never be reported: {line!r}"
    assert match.group("repo") == repo
    assert match.group("ref") == ref


@pytest.mark.parametrize(
    "line",
    [
        # A local action is versioned with the repository that contains it. There is no
        # upstream commit to resolve, so rewriting it would break the workflow.
        pytest.param("      - uses: ./.github/actions/setup", id="local-action"),
        pytest.param("      - uses: ./local-action@v1", id="local-action-with-ref"),
        pytest.param("        uses: ./local-action@v1", id="indented-local-action"),
        # Not a `uses:` key at all.
        pytest.param("      # uses: actions/checkout@v6", id="commented-out"),
        pytest.param("      default-uses: actions/checkout@v6", id="different-key"),
        pytest.param("      - uses: docker://alpine:3.18", id="docker-image"),
    ],
)
def test_a_line_the_matcher_must_leave_alone(line):
    assert pin_actions.PATTERN.match(line) is None, line


def test_every_uses_line_in_the_repository_is_recognised():
    """The regression this test file exists for.

    Counted rather than enumerated: a hard-coded 40 would have to be edited every time a
    step is added, and an outdated number is how this comparison would stop being made.
    """
    seen = 0
    naive = 0
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        expected = naive_uses_lines(text)
        matched = [lineno for lineno, _ in pin_actions.references(text)]
        assert matched == expected, (
            f"{path.name}: the matcher sees {matched}, a naive reader sees {expected}; "
            "the difference is references no check can ever report"
        )
        seen += len(matched)
        naive += len(expected)
    assert naive > 0, "no `uses:` lines found at all -- the workflow path is wrong"
    assert seen == naive


def test_every_reference_in_the_repository_is_pinned_to_a_full_sha():
    """Part two of Issue #15: the references themselves, not just the matcher."""
    floating = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for lineno, match in pin_actions.references(path.read_text(encoding="utf-8")):
            if not re.fullmatch(r"[0-9a-f]{40}", match.group("ref")):
                floating.append(f"{path.name}:{lineno} {match.group('repo')}@{match.group('ref')}")
    assert floating == [], floating


def test_the_check_mode_agrees_with_this_file():
    assert pin_actions.check() == 0


@pytest.mark.parametrize(
    "reference,repo",
    [
        pytest.param("actions/checkout", "actions/checkout", id="two-segment"),
        # `repos/github/codeql-action/init` is a 404: `init` is a directory in the
        # repository, not a repository. Resolving the sub-path verbatim aborts the run.
        pytest.param("github/codeql-action/init", "github/codeql-action", id="sub-path"),
        pytest.param("github/codeql-action/analyze", "github/codeql-action", id="sub-path-sibling"),
        pytest.param("owner/repo/.github/workflows/ci.yml", "owner/repo", id="reusable-workflow"),
    ],
)
def test_a_reference_resolves_against_its_owning_repository(reference, repo):
    assert pin_actions.action_repo(reference) == repo


def test_an_existing_comment_is_preserved_verbatim():
    """The comment is the human-readable half of a pinned reference; a rewrite must not eat it."""
    line = "      - uses: actions/checkout@v5 # v5, do not bump before Issue #99"
    match = pin_actions.PATTERN.match(line)
    assert match is not None
    rewritten = pin_actions.rewritten(match, "d23441a48e516b6c34aea4fa41551a30e30af803")
    assert rewritten == (
        "      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
        " # v5, do not bump before Issue #99"
    )


def test_a_reference_without_a_comment_gains_one_naming_the_tag():
    """A bare 40-hex SHA says nothing about which version it is; the tag has to survive."""
    match = pin_actions.PATTERN.match("        uses: github/codeql-action/init@v4")
    assert match is not None
    rewritten = pin_actions.rewritten(match, "ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd")
    assert rewritten == (
        "        uses: github/codeql-action/init@ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd # v4"
    )


def test_an_already_pinned_reference_is_resolved_without_the_network():
    """A full SHA short-circuits, so a re-run neither calls the API nor rewrites the file."""
    sha = "d23441a48e516b6c34aea4fa41551a30e30af803"  # pragma: allowlist secret
    assert pin_actions.resolve("actions/checkout", sha) == sha
    line = f"      - uses: actions/checkout@{sha} # v6"
    match = pin_actions.PATTERN.match(line)
    assert match is not None
    assert pin_actions.rewritten(match, sha) == line
