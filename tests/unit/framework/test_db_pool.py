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


# --- `isolation=`: the pool owns the session setting, so nothing outlives the checkout ---


def _sql(conn):
    return [c[0][0] for c in conn.cursor.return_value.__enter__.return_value
            .execute.call_args_list]


def test_the_level_is_set_for_the_whole_checkout_and_restored_at_return():
    """`SET TRANSACTION` alone would bind only the NEXT transaction and revert at the
    first commit - a borrower that commits twice would silently finish at the server
    default. The pool sets the SESSION level and undoes it, which is what keeps the
    "nothing outlives the checkout" rule true (TST-2 guards the prohibition; this guards
    the one exception the pool itself owns)."""
    open_ = _opener()
    with db.pooled(CFG, open_, isolation="READ COMMITTED") as conn:
        pass
    statements = _sql(conn)
    assert "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED" in statements
    assert any("transaction_isolation = @@GLOBAL.transaction_isolation" in s
               for s in statements)
    assert statements.index("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED") < \
        next(i for i, s in enumerate(statements) if "@@GLOBAL" in s)


def test_a_borrow_without_isolation_touches_nothing():
    open_ = _opener()
    with db.pooled(CFG, open_) as conn:
        pass
    assert not [s for s in _sql(conn) if "ISOLATION" in s.upper()]


def test_a_fresh_connection_that_cannot_be_prepared_is_closed():
    """It fails BEFORE the yield, where the cleanup has not armed yet: without an explicit
    close the connection would be neither closed nor pooled - a leak per borrow."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = OSError("gone")
    open_ = MagicMock(side_effect=lambda _cfg: conn)

    with pytest.raises(OSError), db.pooled(CFG, open_, isolation="READ COMMITTED"):
        pass

    conn.close.assert_called_once()


def test_a_replacement_opened_after_an_idle_one_failed_preparation_is_closed_too():
    """Two connections are in play on this path; neither may leak."""
    stale, replacement = MagicMock(), MagicMock()
    replacement.cursor.return_value.__enter__.return_value.execute.side_effect = \
        OSError("gone")
    # ONE opener: the pool is keyed by it, so two MagicMocks would be two pools and the
    # idle connection would never be fetched at all.
    open_ = MagicMock(side_effect=[stale, replacement])
    with db.pooled(CFG, open_):
        pass
    stale.cursor.return_value.__enter__.return_value.execute.side_effect = OSError("dead")

    with pytest.raises(OSError), db.pooled(CFG, open_, isolation="READ COMMITTED"):
        pass

    stale.close.assert_called_once()
    replacement.close.assert_called_once()


def _raw_connection_users(tree, text):
    """Methods calling `get_connection(self._db_config)` - by CALL STRUCTURE, not text.

    A substring check passes the moment someone writes `get_connection( self._db_config )`,
    which is exactly the bypass the guard exists to catch.
    """
    import ast as _ast

    def is_bypass(node):
        return (isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Name)
                and node.func.id == "get_connection"
                and any(isinstance(a, _ast.Attribute) and a.attr == "_db_config"
                        and isinstance(a.value, _ast.Name) and a.value.id == "self"
                        for a in node.args))

    return {node.name for node in _ast.walk(tree)
            if isinstance(node, _ast.FunctionDef)
            and any(is_bypass(child) for child in _ast.walk(node))}


def _consumer_tree():
    import ast as _ast
    from pathlib import Path as _Path

    from agento.framework import consumer as _consumer

    text = _Path(_consumer.__file__).read_text()
    return _ast.parse(text), text


def test_the_consumer_borrows_every_connection_at_read_committed():
    """CLS-1: the finalizer hook runs in EVERY transition - success, failure, abandon,
    retry and stale-job recovery - so the level belongs to `Consumer._db()`, which is the
    one funnel all of them pass through. A path that opens its own connection instead
    (recovery used to) silently runs the ranged cleanup at REPEATABLE READ, with the gap
    locks that deadlock at scale, and no test of that path would notice.

    Asserted on the AST, because the alternative - a live two-connection test per
    transition - is five fixtures proving one line. The one method allowed to open its own
    connection is `_maybe_reload_bootstrap`: it re-reads the module registry and reaches
    no finalizer.
    """
    import ast as _ast

    tree, text = _consumer_tree()
    assert _raw_connection_users(tree, text) - {"_maybe_reload_bootstrap"} == set()

    db_method = next(n for n in _ast.walk(tree)
                     if isinstance(n, _ast.FunctionDef) and n.name == "_db")
    borrows = [
        node for node in _ast.walk(db_method)
        if isinstance(node, _ast.Call)
        and any(kw.arg == "isolation" and isinstance(kw.value, _ast.Constant)
                and kw.value.value == "READ COMMITTED" for kw in node.keywords)
    ]
    assert len(borrows) == 1, _ast.dump(db_method)


def test_the_raw_connection_guard_sees_through_whitespace():
    """The guard it guards: a text search passes `get_connection( self._db_config )`."""
    import ast as _ast

    bypass = _ast.parse(
        "class C:\n"
        "    def sneaky(self):\n"
        "        conn = get_connection( self._db_config )\n"
    )
    assert _raw_connection_users(bypass, "") == {"sneaky"}
