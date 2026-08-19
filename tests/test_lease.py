"""The work lease, which stops two Claude Code sessions mutating one Issue at once.

Written after Issue #6 was filed claiming this subsystem was inert and closed as incorrect: it
is acquired at SessionStart by `.claude/hooks/session_context.py` and checked before every
mutation by `pre_tool_policy.lease_conflict`. Part of why a wrong story about it survived an
afternoon is that no test described what it does, so reading the call graph was the only way to
find out and it was possible to read it badly.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

# E402: the hooks directory is not an importable package, so sys.path has to be extended
# first. Suppressed for that reason alone, matching tests/test_pre_tool_policy.py.
import common  # noqa: E402

TTL = 8 * 60 * 60


def test_an_unheld_issue_is_acquired(tmp_path):
    owned, lease = common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    assert owned is True
    assert lease["issue"] == 7
    assert lease["session_id"] == "session-a"


def test_the_owning_session_keeps_it(tmp_path):
    """Re-entry must not look like a conflict with itself."""
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    owned, _ = common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    assert owned is True


def test_the_acquisition_time_survives_re_entry(tmp_path):
    """`acquired` says when the work started; only `heartbeat` moves."""
    _, first = common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)
    _, second = common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    assert second["acquired"] == first["acquired"]


def test_a_live_lease_refuses_another_session(tmp_path):
    """The case the whole subsystem exists for."""
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    owned, held = common.acquire_or_check_lease(tmp_path, 7, "session-b", TTL)

    assert owned is False
    assert held["session_id"] == "session-a", "the refusal must name who holds it"


def test_a_stale_lease_is_taken_over(tmp_path):
    """A session that died must not lock the Issue forever."""
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)
    path = common.lease_path(tmp_path, 7)
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["heartbeat"] = int(time.time()) - TTL - 1
    path.write_text(json.dumps(stale), encoding="utf-8")

    owned, lease = common.acquire_or_check_lease(tmp_path, 7, "session-b", TTL)

    assert owned is True
    assert lease["session_id"] == "session-b"


def test_two_issues_do_not_share_a_lease(tmp_path):
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    owned, _ = common.acquire_or_check_lease(tmp_path, 8, "session-b", TTL)

    assert owned is True


def test_a_foreign_live_lease_is_reported(tmp_path):
    """What `pre_tool_policy.lease_conflict` reads to refuse a mutation."""
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    assert common.foreign_lease(tmp_path, 7, "session-b", TTL) is not None


def test_your_own_lease_is_not_foreign(tmp_path):
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)

    assert common.foreign_lease(tmp_path, 7, "session-a", TTL) is None


def test_a_stale_foreign_lease_does_not_block(tmp_path):
    common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)
    path = common.lease_path(tmp_path, 7)
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["heartbeat"] = int(time.time()) - TTL - 1
    path.write_text(json.dumps(stale), encoding="utf-8")

    assert common.foreign_lease(tmp_path, 7, "session-b", TTL) is None


def test_no_issue_means_no_conflict(tmp_path):
    """Read-only work on a branch naming no Issue is not contended."""
    assert common.foreign_lease(tmp_path, None, "session-b", TTL) is None


def test_unreadable_lease_state_is_treated_as_absent(tmp_path):
    """A half-written file must not lock the Issue out or raise inside a hook."""
    path = common.lease_path(tmp_path, 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert common.read_lease(tmp_path, 7) is None
    assert common.acquire_or_check_lease(tmp_path, 7, "session-a", TTL)[0] is True
