"""`db.pooled`: a bounded idle pool per config (RULES.md SCL-1, TST-2)."""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework import db
from agento.framework.database_config import DatabaseConfig

CFG = DatabaseConfig(mysql_host="h", mysql_user="u")


def _opener():
    return MagicMock(side_effect=lambda _cfg: MagicMock())


def test_a_returned_connection_is_reused_and_rolled_back():
    open_ = _opener()
    with db.pooled(CFG, open_) as first:
        pass
    with db.pooled(CFG, open_) as second:
        pass
    assert second is first
    assert open_.call_count == 1
    assert first.rollback.call_count == 2  # every return drops an open transaction
    first.ping.assert_called_once_with(reconnect=True)  # checked at checkout
    first.close.assert_not_called()


def test_checkout_never_blocks_and_the_idle_cap_closes_the_rest(monkeypatch):
    monkeypatch.setattr(db, "IDLE_CAP", 1)
    open_ = _opener()
    with db.pooled(CFG, open_) as a, db.pooled(CFG, open_) as b:
        assert a is not b  # nothing idle: a second connection, not a wait
    assert open_.call_count == 2
    assert [a.close.called, b.close.called].count(True) == 1


def test_a_connection_that_raised_is_closed_not_pooled():
    open_ = _opener()
    with pytest.raises(RuntimeError), db.pooled(CFG, open_) as conn:
        raise RuntimeError("boom")
    conn.close.assert_called_once()
    with db.pooled(CFG, open_) as again:
        assert again is not conn


def test_a_dead_idle_connection_is_replaced():
    open_ = _opener()
    with db.pooled(CFG, open_) as stale:
        pass
    stale.ping.side_effect = OSError("gone")
    with db.pooled(CFG, open_) as fresh:
        assert fresh is not stale
    stale.close.assert_called_once()


def test_one_pool_per_config():
    open_ = _opener()
    with db.pooled(CFG, open_) as a:
        pass
    with db.pooled(DatabaseConfig(mysql_host="other"), open_) as b:
        assert b is not a


def test_no_pooled_connection_where_session_state_is_set():
    """TST-2: GET_LOCK and SET SESSION outlive the checkout, so the next borrower would
    inherit a lock or a session setting. A file that uses one never uses `pooled`."""
    src = Path(db.__file__).resolve().parents[1]
    offenders = [
        str(p.relative_to(src)) for p in src.rglob("*.py")
        if p.name != "db.py"
        and "pooled(" in (text := p.read_text())
        and re.search(r"GET_LOCK|SET SESSION", text)
    ]
    assert offenders == []
