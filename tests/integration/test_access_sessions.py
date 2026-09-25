"""Panel sessions against a real MySQL (framework/access/sessions.py)."""
from __future__ import annotations

import pytest

from agento.framework.access import accounts, sessions

from .conftest import _test_connection

PASSWORD = "correct horse battery"


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    _clean(c)
    yield c
    _clean(c)
    c.close()


def _clean(c):
    with c.cursor() as cur:
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


@pytest.fixture
def alice(conn):
    return accounts.create_user(conn, "alice", "user", PASSWORD)


def test_create_then_lookup(conn, alice):
    session, token = sessions.create_session(conn, alice)
    found = sessions.lookup_session(conn, token)
    assert found == session and found.user == alice


def test_wrong_or_empty_token(conn, alice):
    sessions.create_session(conn, alice)
    assert sessions.lookup_session(conn, "not-the-token") is None
    assert sessions.lookup_session(conn, "") is None
    assert sessions.lookup_session(conn, None) is None


def test_revoked(conn, alice):
    session, token = sessions.create_session(conn, alice)
    sessions.revoke_session(conn, session.id)
    assert sessions.lookup_session(conn, token) is None


def test_expired_is_checked_by_the_database_clock(conn, alice):
    session, token = sessions.create_session(conn, alice)
    with conn.cursor() as cur:
        cur.execute("UPDATE session SET expires_at = NOW() - INTERVAL 1 SECOND WHERE id = %s", (session.id,))
    assert sessions.lookup_session(conn, token) is None


def test_inactive_user_and_role_change(conn, alice):
    _, token = sessions.create_session(conn, alice)
    accounts.set_role(conn, alice.id, "admin")
    assert sessions.lookup_session(conn, token) is None
    _, token = sessions.create_session(conn, accounts.get_user(conn, alice.id))
    with conn.cursor() as cur:
        cur.execute("UPDATE `user` SET is_active = 0 WHERE id = %s", (alice.id,))
    assert sessions.lookup_session(conn, token) is None


def test_lifetime_follows_session_max_ttl(conn, alice, monkeypatch):
    monkeypatch.setenv("CONFIG__CORE__AUTH__SESSION_MAX_TTL", "60")
    session, _ = sessions.create_session(conn, alice)
    with conn.cursor() as cur:
        cur.execute("SELECT TIMESTAMPDIFF(SECOND, NOW(), expires_at) AS left_s FROM session WHERE id = %s",
                    (session.id,))
        assert 55 <= cur.fetchone()["left_s"] <= 60


def test_csrf_token_is_bound_to_its_session(conn, alice):
    _, token = sessions.create_session(conn, alice)
    _, other = sessions.create_session(conn, alice)
    assert sessions.csrf_valid(token, sessions.csrf_token(token))
    assert not sessions.csrf_valid(token, sessions.csrf_token(other))
    assert not sessions.csrf_valid(token, None)
    assert not sessions.csrf_valid(token, "")


def test_no_raw_token_in_the_table(conn, alice):
    _, token = sessions.create_session(conn, alice)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM session")
        rows = cur.fetchall()
    assert rows and all(token not in str(v) for row in rows for v in row.values())
