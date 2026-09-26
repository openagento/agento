"""Shared fixtures for the framework/access integration tests."""
from __future__ import annotations

import pytest

from .conftest import _test_connection


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
def scopes(conn):
    """ws1 holds av1, ws2 holds av2 — the fixture's symbolic scopes."""
    ids = {}
    with conn.cursor() as cur:
        for ws, av in (("ws1", "av1"), ("ws2", "av2")):
            cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES (%s, %s)", (f"e2-{ws}", ws))
            cur.execute("SELECT id FROM workspace WHERE code = %s", (f"e2-{ws}",))
            ids[ws] = cur.fetchone()["id"]
            cur.execute(
                "INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)",
                (ids[ws], f"e2-{av}", av),
            )
            cur.execute("SELECT id FROM agent_view WHERE code = %s", (f"e2-{av}",))
            ids[av] = cur.fetchone()["id"]
    return ids
