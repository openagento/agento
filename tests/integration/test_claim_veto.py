"""The pre-claim seam: `job_claim_before`, the clamped delay and the defer stretch (§4.4).

This is the one dispatch in the framework that is not fail-open, and the one place where a
wrong transaction boundary would lose the ordering guarantee - so the tests here are about
the boundary as much as about the verdict.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from agento.framework.consumer import Consumer
from agento.framework.defer import (
    DEFER_CEILING_MS,
    DEFER_FLOOR_MS,
    clamp_delay,
    defer_seconds,
    prune_defer_stretches,
)
from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.events import ClaimVerdict

from .conftest import _test_connection

EVENT = "job_claim_before"


@pytest.fixture
def observers():
    """Register observers for one test and leave the registry as it was."""
    em = get_event_manager()
    before = list(em._observers.get(EVENT, []))
    registered: list = []

    def add(observer_class, name="test"):
        em.register(EVENT, ObserverEntry(name=name, observer_class=observer_class))
        registered.append(name)

    yield add
    em._observers[EVENT] = before


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    with c.cursor() as cur:
        cur.execute("DELETE FROM job_defer_stretch")
        cur.execute("DELETE FROM job_event_outbox")
    c.commit()
    yield c
    c.close()


def _job(conn, key="veto:1") -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, reference_id, idempotency_key, status) "
                    "VALUES ('blank', 'test', 'V-1', %s, 'TODO')", (key,))
        job_id = cur.lastrowid
    conn.commit()
    return job_id


def _rows(conn, sql, params=()) -> list[dict]:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


class Allowing:
    def execute(self, event) -> None:
        return None


class Vetoing:
    def execute(self, event) -> None:
        event.verdict = ClaimVerdict.DEFER


class Raising:
    def execute(self, event) -> None:
        raise RuntimeError("the observer broke")


def _delaying(delay):
    class Delaying:
        def execute(self, event) -> None:
            event.verdict = ClaimVerdict.DEFER
            event.delay_ms = delay
    return Delaying


# --- the three-case verdict table -----------------------------------------

def test_no_observer_allows_the_claim(conn, int_db_config, int_consumer_config):
    job_id = _job(conn)

    job = _consumer(int_db_config, int_consumer_config)._try_dequeue()

    assert job is not None and job.id == job_id
    assert _rows(conn, "SELECT status, attempt FROM job")[0] == {"status": "RUNNING", "attempt": 1}


def test_an_observer_that_sets_no_verdict_allows_the_claim(
    conn, observers, int_db_config, int_consumer_config
):
    observers(Allowing)
    _job(conn)

    assert _consumer(int_db_config, int_consumer_config)._try_dequeue() is not None


def test_a_vetoing_observer_defers(conn, observers, int_db_config, int_consumer_config):
    observers(Vetoing)
    _job(conn)

    assert _consumer(int_db_config, int_consumer_config)._try_dequeue() is None


def test_a_raising_observer_defers_and_is_logged(
    conn, observers, int_db_config, int_consumer_config, caplog
):
    """An observer that cannot decide has not decided yes."""
    observers(Raising)
    _job(conn)

    with caplog.at_level(logging.ERROR):
        assert _consumer(int_db_config, int_consumer_config)._try_dequeue() is None

    assert any("the observer broke" in r.getMessage() or r.exc_info for r in caplog.records)


def test_a_deferred_job_keeps_its_attempt_count_and_never_went_running(
    conn, observers, int_db_config, int_consumer_config
):
    _job(conn)
    observers(Vetoing)

    _consumer(int_db_config, int_consumer_config)._try_dequeue()

    row = _rows(conn, "SELECT status, attempt, started_at FROM job")[0]
    assert row == {"status": "TODO", "attempt": 0, "started_at": None}


def test_the_deferral_advances_scheduled_after_in_the_same_transaction(
    conn, observers, int_db_config, int_consumer_config
):
    _job(conn)
    observers(Vetoing)

    _consumer(int_db_config, int_consumer_config)._try_dequeue()

    # Committed and in the future: the decision and the delay are one transaction.
    assert _rows(conn, "SELECT id FROM job WHERE scheduled_after >= NOW() + INTERVAL 1 SECOND")


# --- the delay is clamped, never trusted ----------------------------------

@pytest.mark.parametrize("delay,expected", [
    (0, DEFER_FLOOR_MS), (None, DEFER_FLOOR_MS), (-5, DEFER_FLOOR_MS),
    (10 ** 9, DEFER_CEILING_MS), ("nonsense", DEFER_FLOOR_MS),
    (1000, 1000), (DEFER_FLOOR_MS, DEFER_FLOOR_MS), (DEFER_CEILING_MS, DEFER_CEILING_MS),
])
def test_the_delay_is_clamped(delay, expected):
    assert clamp_delay(delay) == expected


@pytest.mark.parametrize("delay,expected", [(0, 1), (None, 1), (250, 1), (1001, 2), (10 ** 9, 30)])
def test_the_clamped_delay_is_whole_seconds_rounded_up(delay, expected):
    """`scheduled_after` is a second-precision TIMESTAMP: rounding down is no delay at all."""
    assert defer_seconds(delay) == expected


@pytest.mark.parametrize("delay", [0, None, 10 ** 9])
def test_a_clamped_delay_reaches_the_row(
    conn, observers, int_db_config, int_consumer_config, delay
):
    """Whatever the observer asks for, the row lands inside the framework's bounds."""
    _job(conn)
    observers(_delaying(delay))
    before = _rows(conn, "SELECT NOW() AS t")[0]["t"]

    _consumer(int_db_config, int_consumer_config)._try_dequeue()

    # Measured from before the claim, so the test's own elapsed time cannot eat the floor.
    scheduled = _rows(conn, "SELECT scheduled_after AS t FROM job")[0]["t"]
    ahead_ms = (scheduled - before).total_seconds() * 1000
    assert 1000 <= ahead_ms <= DEFER_CEILING_MS + 1000


def test_the_framework_names_no_module_config_path():
    """PLC-2: the delay travels in the verdict; the framework never reads `conversation/*`."""
    found = subprocess.run(
        ["grep", "-rn", "conversation/", "src/agento/framework/"],
        capture_output=True, text=True,
    )
    assert found.stdout == "", found.stdout


# --- the stretch: one block, one event ------------------------------------

def test_no_event_fires_on_the_deferral_itself(
    conn, observers, int_db_config, int_consumer_config
):
    _job(conn)
    observers(Vetoing)

    _consumer(int_db_config, int_consumer_config)._try_dequeue()

    assert _rows(conn, "SELECT kind FROM job_event_outbox") == []
    stretch = _rows(conn, "SELECT stretch_seq, defer_count, closed_at FROM job_defer_stretch")
    assert stretch == [{"stretch_seq": 1, "defer_count": 1, "closed_at": None}]


def test_many_deferrals_are_one_stretch_announced_once_when_it_closes(
    conn, observers, int_db_config, int_consumer_config
):
    job_id = _job(conn)
    add = observers
    add(Vetoing)
    consumer = _consumer(int_db_config, int_consumer_config)

    for _ in range(4):
        with conn.cursor() as cur:       # undo the backoff so the next tick sees it
            cur.execute("UPDATE job SET scheduled_after = NOW() WHERE id = %s", (job_id,))
        conn.commit()
        assert consumer._try_dequeue() is None

    assert _rows(conn, "SELECT kind FROM job_event_outbox") == []
    assert _rows(conn, "SELECT defer_count FROM job_defer_stretch")[0]["defer_count"] == 4

    # The block ends: the claim that ends it announces itself AND the stretch, once each.
    get_event_manager()._observers["job_claim_before"] = []
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET scheduled_after = NOW() WHERE id = %s", (job_id,))
    conn.commit()
    assert consumer._try_dequeue() is not None

    events = _rows(conn, "SELECT kind, payload FROM job_event_outbox")
    # `job.claimed` is written in the same claim transaction (PRD E3-E5 §6.4.2); the point
    # of this test is that the four deferrals are ONE `job.deferred`, not four.
    deferred = [e for e in events if e["kind"] == "job.deferred"]
    assert [e["kind"] for e in events] == ["job.claimed", "job.deferred"]
    import json
    assert json.loads(deferred[0]["payload"])["defer_count"] == 4
    assert _rows(conn, "SELECT closed_at FROM job_defer_stretch")[0]["closed_at"] is not None


def test_a_replayed_close_announces_nothing_more(conn):
    from agento.framework.defer import close_stretch, open_or_bump_stretch

    job_id = _job(conn)
    with conn.cursor() as cur:
        open_or_bump_stretch(cur, job_id=job_id)
        assert close_stretch(cur, job_id=job_id) is not None
        assert close_stretch(cur, job_id=job_id) is None
    conn.commit()

    assert len(_rows(conn, "SELECT id FROM job_event_outbox")) == 1


def test_an_unrelated_job_is_claimed_without_waiting_for_a_deferred_one(
    conn, observers, int_db_config, int_consumer_config
):
    """The starvation test: a deferral holds its own job, not the queue."""
    blocked = _job(conn, key="veto:blocked")
    free = _job(conn, key="veto:free")

    class VetoOne:
        def execute(self, event) -> None:
            if event.job_id == blocked:
                event.verdict = ClaimVerdict.DEFER

    observers(VetoOne)
    consumer = _consumer(int_db_config, int_consumer_config)

    assert consumer._try_dequeue() is None          # the blocked one was offered first
    claimed = consumer._try_dequeue()

    assert claimed is not None and claimed.id == free


# --- the prune reaches the second table -----------------------------------

def _stretch(conn, *, job_id, closed_days_ago=None, opened_days_ago=0) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job_defer_stretch (job_id, stretch_seq, defer_count, opened_at, closed_at) "
            "VALUES (%s, 1, 1, NOW() - INTERVAL %s DAY, "
            "IF(%s IS NULL, NULL, NOW() - INTERVAL IFNULL(%s, 0) DAY))",
            (job_id, opened_days_ago, closed_days_ago, closed_days_ago),
        )
        row_id = cur.lastrowid
    conn.commit()
    return row_id


def test_a_closed_stretch_past_the_window_is_pruned(conn):
    _stretch(conn, job_id=1, closed_days_ago=40)

    assert prune_defer_stretches(conn, retention_days=30) == 1
    assert _rows(conn, "SELECT id FROM job_defer_stretch") == []


def test_a_closed_stretch_inside_the_window_survives(conn):
    _stretch(conn, job_id=1, closed_days_ago=5)

    assert prune_defer_stretches(conn, retention_days=30) == 0


def test_an_open_stretch_is_never_pruned_however_old(conn):
    """A live block must not have its stretch taken out from under it."""
    from agento.framework.defer import close_stretch

    job_id = _job(conn)
    _stretch(conn, job_id=job_id, opened_days_ago=400)

    assert prune_defer_stretches(conn, retention_days=1) == 0

    with conn.cursor() as cur:
        assert close_stretch(cur, job_id=job_id) is not None
    conn.commit()
    assert len(_rows(conn, "SELECT id FROM job_event_outbox")) == 1


def test_one_tick_prunes_both_tables(conn):
    from agento.framework.outbox import prune_outbox, write_outbox

    _stretch(conn, job_id=1, closed_days_ago=40)
    with conn.cursor() as cur:
        write_outbox(cur, job_id=1, kind="job.deferred", payload={"n": 1})
        cur.execute("UPDATE job_event_outbox SET created_at = NOW() - INTERVAL 40 DAY")
    conn.commit()

    assert prune_outbox(conn, retention_days=30) == 1
    assert prune_defer_stretches(conn, retention_days=30) == 1
