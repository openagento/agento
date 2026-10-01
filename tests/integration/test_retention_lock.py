"""The retention lock and the live-launch set against real MySQL (PRD E6 §5).

The toolbox prune takes the same named lock with ``GET_LOCK(name, 0)`` and keeps the
versions ``live_launch_versions_sql`` returns (fixture retention_lock_v1.json).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.access import accounts, launches

from .conftest import _test_connection

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "retention_lock_v1.json").read_text())
LIVE_SQL = FIXTURE["live_launch_versions_sql"].replace("?", "%s")
V1, V2 = "v-20260101-000000-aaaa", "v-20260102-000000-bbbb"


@pytest.fixture
def conns():
    a, b = _test_connection(autocommit=True), _test_connection(autocommit=True)
    _clean(a)
    yield a, b
    _clean(a)
    a.close()
    b.close()


def _clean(c):
    with c.cursor() as cur:
        for table in ("launch", "session", "role_grant", "`user`"):
            cur.execute(f"DELETE FROM {table}")


def _prune_try(conn, code) -> bool:
    """What the toolbox prune does first: take the lock without waiting."""
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 0) AS got", (launches.retention_lock_name(code),))
        return cur.fetchone()["got"] == 1


def _prune_release(conn, code):
    with conn.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(%s)", (launches.retention_lock_name(code),))


def test_a_launch_waits_for_a_running_prune_and_a_prune_skips_a_running_launch(conns, monkeypatch):
    prune, web = conns
    monkeypatch.setattr(launches, "RETENTION_LOCK_WAIT_SECONDS", 1)
    assert _prune_try(prune, "site")
    with pytest.raises(launches.RetentionBusy), launches.retention_lock(web, "site"):
        pass
    _prune_release(prune, "site")
    with launches.retention_lock(web, "site"):
        assert not _prune_try(prune, "site")
        assert _prune_try(prune, "other")  # one lock per artifact
        _prune_release(prune, "other")
    assert _prune_try(prune, "site")  # released on exit
    _prune_release(prune, "site")


def _live(conn, code) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(LIVE_SQL, (code,))
        return {r["version_id"] for r in cur.fetchall()}


def test_the_prune_keeps_exactly_the_versions_live_launches_pin(conns, monkeypatch):
    conn, _ = conns
    monkeypatch.setattr(launches, "has_operation", lambda *a: True)
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('e6-ret', 'e6')")
        cur.execute("SELECT id FROM workspace WHERE code = 'e6-ret'")
        ws = cur.fetchone()["id"]
    user = accounts.create_user(conn, "alice", "user", "correct horse battery")
    kept, _ = launches.create_launch(conn, user, workspace_id=ws, agent_view_id=None, artifact_code="site",
                                     version_id=V1)
    ended, _ = launches.create_launch(conn, user, workspace_id=ws, agent_view_id=None, artifact_code="site",
                                      version_id=V2)
    launches.create_launch(conn, user, workspace_id=ws, agent_view_id=None, artifact_code="other", version_id=V2)
    assert _live(conn, "site") == {V1, V2}
    assert launches.revoke_launch(conn, user, ended.id)
    assert _live(conn, "site") == {V1}
    with conn.cursor() as cur:
        cur.execute("UPDATE launch SET expires_at = NOW() - INTERVAL 1 SECOND WHERE id = %s", (kept.id,))
    assert _live(conn, "site") == set()
