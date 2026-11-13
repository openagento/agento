"""The §7 rights matrix (AG-67 plan §7), decided from the row, the held leaves, the visible
views and the share row."""
from __future__ import annotations

from datetime import datetime

import pytest

from agento.framework.access.accounts import User
from agento.modules.scheduler.src.rights import Rights, decide, group

ME = User(id=1, username="me", role="user", is_active=True)
ADMIN = User(id=9, username="root", role="admin", is_active=True)
VIEW, OTHER_VIEW = 10, 20

N = Rights(False, False, False, False, False)
R = Rights(True, False, False, False, False)
RW = Rights(True, True, False, False, False)
RS = Rights(True, False, True, True, False)
ALL = Rights(True, True, True, True, True)


def _row(**over) -> dict:
    row = {"id": 5, "source": "scheduler", "agent_view_id": VIEW, "owner_user_id": None,
           "owner_agent_view_id": None, "deleted_at": None}
    row.update(over)
    return row


MINE = _row(owner_user_id=ME.id)
AGENT = _row(owner_agent_view_id=VIEW)
THEIRS = _row(owner_user_id=2)
JIRA = _row(source="jira_periodic", owner_agent_view_id=VIEW)


@pytest.mark.parametrize("row, held, visible, member, expected", [
    (MINE, set(), {VIEW}, False, N),
    (MINE, {"scheduler.own"}, set(), False, R),
    (MINE, {"scheduler.own", "scheduler.own.write"}, set(), False, RW),
    (MINE, {"scheduler.own", "scheduler.own.share"}, set(), False, RS),
    (MINE, {"scheduler.own.write", "scheduler.own.share"}, set(), False, N),  # no read: nothing
    (THEIRS, set(), set(), True, RW),                                        # a share: edit only (D-3)
    (AGENT, {"scheduler.agent"}, {VIEW}, False, R),
    (AGENT, {"scheduler.agent", "scheduler.agent.write"}, {VIEW}, False, RW),
    (AGENT, {"scheduler.agent", "scheduler.agent.share"}, {VIEW}, False, RS),
    (AGENT, {"scheduler.agent", "scheduler.agent.write", "scheduler.agent.share"}, {OTHER_VIEW}, False, N),
    (AGENT, {"scheduler.user", "scheduler.own"}, {VIEW}, False, N),
    (THEIRS, {"scheduler.user"}, {VIEW}, False, R),
    (THEIRS, {"scheduler.user", "scheduler.user.write"}, {VIEW}, False, RW),
    (THEIRS, {"scheduler.user", "scheduler.user.share"}, {VIEW}, False, RS),
    (THEIRS, {"scheduler.user", "scheduler.user.write"}, {OTHER_VIEW}, False, N),
    (THEIRS, {"scheduler.agent", "scheduler.own"}, {VIEW}, False, N),
    (JIRA, {"scheduler.agent", "scheduler.agent.write", "scheduler.agent.share"}, {VIEW}, False, R),
    (_row(owner_user_id=ME.id, deleted_at=datetime(2026, 1, 1)), {"scheduler.own"}, {VIEW}, False, N),
])
def test_the_matrix(row, held, visible, member, expected):
    assert decide(ME, row, held=held, visible=visible, member=member) == expected


@pytest.mark.parametrize("row, expected", [
    (MINE, ALL), (AGENT, ALL), (THEIRS, ALL), (JIRA, R),
    (_row(owner_user_id=2, deleted_at=datetime(2026, 1, 1)), N),
])
def test_admin_holds_everything_but_a_read_only_source_and_a_deleted_row(row, expected):
    assert decide(ADMIN, row, held=set(), visible=set(), member=False) == expected


def test_change_owner_is_admin_only():
    every = {f"scheduler.{g}{s}" for g in ("own", "agent", "user") for s in ("", ".write", ".share")}
    assert not decide(ME, MINE, held=every, visible={VIEW}, member=True).change_owner


def test_an_inactive_user_holds_nothing():
    gone = User(id=1, username="me", role="admin", is_active=False)
    assert decide(gone, MINE, held={"scheduler.own"}, visible={VIEW}, member=True) == N


@pytest.mark.parametrize("row, member, expected", [
    (MINE, True, "mine"), (THEIRS, True, "shared"), (AGENT, False, "agent"), (JIRA, False, "agent"),
    (THEIRS, False, "user"),
])
def test_group(row, member, expected):
    assert group(ME, row, member=member) == expected


def test_a_channel_reference_that_is_not_ascii_digits_finds_no_row():
    from unittest.mock import MagicMock

    from agento.modules.scheduler.src.channel import _row

    cur = MagicMock()
    assert [_row(cur, ref) for ref in ("²", "\u0661", "-1", "")] == [None] * 4
    cur.execute.assert_not_called()
