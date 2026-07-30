"""Control-plane policy tests.

The Dependabot target-branch rule is the one that bit us: without it Dependabot
opens pull requests against the default branch, which is the production branch,
so ci-pr.yml never fires and dependency changes reach production without
passing through integration.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from check_workflow_policy import (  # noqa: E402
    check_dependabot,
    check_dependabot_text,
    integration_branch,
)

ECOSYSTEM = """version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
{target}    schedule:
      interval: "weekly"
"""


def _config(target: str = "") -> str:
    return ECOSYSTEM.format(target=target)


def test_missing_target_branch_is_rejected():
    failures = check_dependabot_text(_config(), "dev")
    assert len(failures) == 1
    assert "sets no target-branch" in failures[0]
    assert "'pip'" in failures[0]


def test_production_target_branch_is_rejected():
    failures = check_dependabot_text(_config('    target-branch: "master"\n'), "dev")
    assert len(failures) == 1
    assert "targets 'master'" in failures[0]
    assert "expected the integration branch 'dev'" in failures[0]


def test_integration_target_branch_passes():
    assert check_dependabot_text(_config('    target-branch: "dev"\n'), "dev") == []


def test_unquoted_target_branch_passes():
    assert check_dependabot_text(_config("    target-branch: dev\n"), "dev") == []


def test_every_ecosystem_is_checked_independently():
    text = """version: 2
updates:
  - package-ecosystem: "pip"
    target-branch: "dev"
    directory: "/"
  - package-ecosystem: "docker"
    directory: "/"
"""
    failures = check_dependabot_text(text, "dev")
    assert len(failures) == 1
    assert "'docker'" in failures[0]


def test_no_ecosystems_is_not_a_failure():
    assert check_dependabot_text("version: 2\nupdates: []\n", "dev") == []


def test_this_repository_satisfies_the_rule():
    """Regression guard: the real dependabot.yml must stay compliant."""
    assert check_dependabot() == []


def test_integration_branch_comes_from_the_workflow_config():
    assert integration_branch() == "dev"
