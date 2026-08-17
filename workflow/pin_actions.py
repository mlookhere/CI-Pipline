#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Both spellings GitHub accepts, matching workflow/check_workflow_policy.py. A workflow this
# reader cannot see is a workflow whose Actions are never pinned and never reported.
WORKFLOW_GLOB = "*.y*ml"
# The owner segment, which must begin with an alphanumeric. That anchor is the whole reason
# local references stay out: the previous pattern read `./local-action@v1` as the repository
# `./local-action`, which resolves to no upstream commit and aborts the run.
OWNER = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
# Every later segment: the repository name, and any directory path inside it. Deliberately
# looser than OWNER, because a leading dot is legal here and appears in the reusable-workflow
# form `owner/repo/.github/workflows/ci.yml@v1`. Requiring the alphanumeric anchor on every
# segment would skip that form silently -- the same defect as the two-segment pattern below.
PATH_SEGMENT = r"[A-Za-z0-9_.-]+"
# `uses:` is a step key, not necessarily the step's first key: `- name:`, `- id:` and `- if:`
# may precede it, leaving `uses:` indented with no dash of its own. Requiring the dash skipped
# 13 of this repository's 40 references, and the two-segment repository pattern that came with
# it skipped the two `github/codeql-action/<tool>` references -- silently, because a line the
# matcher never sees is not a line it can report as unpinned (Issue #15).
PATTERN = re.compile(
    rf"(?P<prefix>\s*(?:-\s+)?uses:\s+)"
    rf"(?P<repo>{OWNER}(?:/{PATH_SEGMENT})+)"
    rf"@(?P<ref>[^\s#]+)(?P<suffix>.*)$"
)
SHA = re.compile(r"[0-9a-fA-F]{40}")


def workflow_files() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob(WORKFLOW_GLOB))


def references(text: str) -> list[tuple[int, re.Match[str]]]:
    """Every external Action reference in `text`, as `(line number, match)`."""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = PATTERN.match(line)
        if match:
            found.append((lineno, match))
    return found


def action_repo(reference: str) -> str:
    """The `owner/repo` the API knows, for a reference that may carry a sub-path.

    `github/codeql-action/init` is a directory published from `github/codeql-action`; asking
    the API for `repos/github/codeql-action/init` returns 404, which would abort the run.
    """
    owner, name, *_ = reference.split("/")
    return f"{owner}/{name}"


def rewritten(match: re.Match[str], sha: str) -> str:
    """The reference line rebuilt around `sha`, keeping any comment the author wrote."""
    suffix = match.group("suffix").rstrip()
    comment = suffix if suffix.strip().startswith("#") else f" # {match.group('ref')}"
    return f"{match.group('prefix')}{match.group('repo')}@{sha}{comment}"


def resolve(repo: str, ref: str) -> str:
    """The commit `ref` names today.

    `repos/{repo}/commits/{ref}` rather than the tag refs API, because a floating Action
    version is not always a lightweight tag: `github/codeql-action@v4` is an annotated tag
    that needs dereferencing, and `actions/dependency-review-action@v4` is a branch, for
    which `git/ref/tags/v4` returns 404. This endpoint answers all three with the commit.
    """
    if SHA.fullmatch(ref):
        return ref.lower()
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"failed to resolve {repo}@{ref}")
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit(f"unexpected SHA for {repo}@{ref}: {sha}")
    return sha


def check() -> int:
    failures: list[str] = []
    inspected = 0
    for path in workflow_files():
        for lineno, match in references(path.read_text(encoding="utf-8")):
            inspected += 1
            if not SHA.fullmatch(match.group("ref")):
                failures.append(
                    f"{path.relative_to(ROOT)}:{lineno}: "
                    f"{match.group('repo')}@{match.group('ref')} is not pinned to a full commit SHA"
                )
    if failures:
        print("Unpinned Actions:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    if not inspected:
        # A check that inspected nothing has proved nothing. Reporting success over an empty
        # set is how this gate stayed green for as long as the reference syntax went
        # unrecognised, so an empty inspection is a failure rather than a pass.
        print("No Action references found to check; the matcher or the path is wrong.", file=sys.stderr)
        return 1
    print(f"All {inspected} external GitHub Action references are pinned to full commit SHAs.")
    return 0


def pin() -> int:
    changed = 0
    for path in workflow_files():
        output: list[str] = []
        touched = False
        for line in path.read_text(encoding="utf-8").splitlines():
            match = PATTERN.match(line)
            if not match:
                output.append(line)
                continue
            ref = match.group("ref")
            sha = resolve(action_repo(match.group("repo")), ref)
            output.append(rewritten(match, sha))
            # A reference that only gains a comment is not a change worth writing; `touched`
            # stays keyed to the SHA so a re-run over pinned files rewrites nothing.
            touched = touched or sha != ref
        if touched:
            # newline="\n" because these files are LF in the repository and this script also
            # runs on Windows, where write_text's default would translate every line ending
            # and turn a 40-line pin into a whole-file rewrite. There is no .gitattributes
            # and core.autocrlf is false, so nothing downstream would correct it.
            path.write_text("\n".join(output) + "\n", encoding="utf-8", newline="\n")
            changed += 1
            print(f"pinned: {path.relative_to(ROOT)}")
    print(f"Pinned external Actions in {changed} workflow file(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="Fail if any Action reference is not a full commit SHA"
    )
    args = parser.parse_args()
    return check() if args.check else pin()


if __name__ == "__main__":
    raise SystemExit(main())
