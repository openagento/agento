"""The tick at scale (AG-67 plan §11 step 12, SCL-1): the batch is a hard bound, concurrent ticks
publish no duplicate, and blocked rows never starve runnable ones."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from agento.framework.schedule import sources, tick

from .conftest import DB_STRESS, _test_connection
from .test_scheduler_tick import _Source

pytestmark = DB_STRESS

NOW = datetime(2026, 6, 1, 12, 0)
BATCH = 100


@pytest.fixture(autouse=True)
def _registered(monkeypatch):
    monkeypatch.setattr(sources, "load_sources", lambda: {"testsrc": _Source()})


@pytest.fixture
def conn():
    c = _test_connection()
    yield c
    c.close()


def _seed(conn, n: int, *, next_fire_at: datetime = NOW, open_job: bool = False) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('scale-ws', 'ws')")
        cur.execute("SELECT id FROM workspace WHERE code = 'scale-ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'scale-av', 'av')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'scale-av'")
        av = cur.fetchone()["id"]
        cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM schedule")
        first = int(cur.fetchone()["m"]) + 1
        cur.executemany(
            "INSERT INTO schedule (id, source, agent_view_id, owner_agent_view_id, rrule, starts_at, timezone, "
            "enabled, next_fire_at) VALUES (%s, 'testsrc', %s, %s, 'FREQ=HOURLY;BYMINUTE=0', %s, 'UTC', 1, %s)",
            [(first + i, av, av, datetime(2026, 6, 1), next_fire_at) for i in range(n)])
        ids = list(range(first, first + n))
        if open_job:
            cur.executemany("INSERT INTO job (type, source, idempotency_key, status, schedule_id) "
                            "VALUES ('blank', 'testsrc', %s, 'RUNNING', %s)", [(f"open-{i}", i) for i in ids])
    conn.commit()
    return ids


def _published(conn) -> dict[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT schedule_id, COUNT(*) AS n FROM job WHERE status = 'TODO' GROUP BY schedule_id")
        rows = {r["schedule_id"]: r["n"] for r in cur.fetchall()}
    conn.rollback()
    return rows


def test_one_tick_publishes_exactly_the_batch_and_three_drain_everything(conn):
    _seed(conn, 250)
    assert tick.tick(conn, now=NOW, batch=BATCH).published == BATCH
    assert sum(_published(conn).values()) == BATCH
    tick.tick(conn, now=NOW, batch=BATCH)
    tick.tick(conn, now=NOW, batch=BATCH)
    published = _published(conn)
    assert len(published) == 250 and set(published.values()) == {1}


def test_two_concurrent_ticks_publish_no_duplicate(conn):
    _seed(conn, 250)
    results: list = []

    def run():
        c = _test_connection()
        try:
            results.append(tick.tick(c, now=NOW, batch=BATCH))
        finally:
            c.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert len(results) == 2
    published = _published(conn)
    assert set(published.values()) == {1}
    assert sum(published.values()) == sum(r.published for r in results) <= 2 * BATCH


def test_blocked_rows_in_front_do_not_starve_runnable_rows(conn):
    _seed(conn, BATCH, next_fire_at=NOW - timedelta(minutes=30), open_job=True)
    runnable = _seed(conn, 50)
    first = tick.tick(conn, now=NOW, batch=BATCH)
    assert (first.skipped, first.published) == (BATCH, 0)
    assert tick.tick(conn, now=NOW, batch=BATCH).published == 50
    assert set(_published(conn)) == set(runnable)
