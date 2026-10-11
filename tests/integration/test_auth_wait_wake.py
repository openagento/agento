"""WI-6 wake bound (RULES.md SCL-1): one wake reads and locks at most WAKE_BATCH rows.

1000 already-due auth-wait jobs, 250 future auth-wait jobs and 2000 unrelated jobs. The
unrelated and the future rows have the lowest ids, so the batch is visible: a wake only
moves a future row; on a due row it is a no-op.
"""
from __future__ import annotations

import threading

import pytest

from agento.modules.core.src.observers import WAKE_BATCH, auth_waiting_ids, wake_auth_waiting_jobs

from .conftest import TEST_DB, _test_connection

MARKER = "CredentialsUnavailableError"
UNRELATED, FUTURE, DUE = 2000, 250, 1000
HANDLER_READS_MAX = WAKE_BATCH + 1  # one index dive + the next entries (the last may peek one)


def _insert(cur, prefix: str, count: int, status: str, error_class: str | None, offset_s: int):
    cur.executemany(
        "INSERT INTO job (type, source, idempotency_key, status, attempt, max_attempts, "
        "error_class, scheduled_after) "
        "VALUES ('cron', 'jira', %s, %s, 0, 3, %s, DATE_ADD(NOW(), INTERVAL %s SECOND))",
        [(f"{prefix}:{i}", status, error_class, offset_s) for i in range(count)],
    )


def _seed() -> tuple[list[int], list[int]]:
    """Returns (future auth-wait ids, due auth-wait ids), ascending."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            _insert(cur, "other", UNRELATED - 20, "TODO", None, 3600)
            _insert(cur, "other-dead", 10, "DEAD", MARKER, 3600)       # marker, not waiting
            _insert(cur, "other-run", 10, "RUNNING", MARKER, 3600)     # marker, not waiting
            _insert(cur, "future", FUTURE, "TODO", MARKER, 3600)
            _insert(cur, "due", DUE, "TODO", MARKER, -60)
            cur.execute("SELECT id, idempotency_key FROM job ORDER BY id")
            rows = cur.fetchall()
    finally:
        conn.close()
    future = [r["id"] for r in rows if r["idempotency_key"].startswith("future:")]
    due = [r["id"] for r in rows if r["idempotency_key"].startswith("due:")]
    return future, due


def _schedule(cur) -> dict[int, object]:
    cur.execute("SELECT id, scheduled_after FROM job")
    return {r["id"]: r["scheduled_after"] for r in cur.fetchall()}


def _handler_reads(cur) -> int:
    cur.execute("SHOW SESSION STATUS LIKE 'Handler_read%'")
    return sum(int(r["Value"]) for r in cur.fetchall())


def _measured_read(cur) -> tuple[list[int], int]:
    """The candidate read and its own Handler_read_* delta. SHOW STATUS may bump the
    counters itself, so two back-to-back readings measure that overhead first."""
    a = _handler_reads(cur)
    overhead = _handler_reads(cur) - a
    before = _handler_reads(cur)
    ids = auth_waiting_ids(cur)
    return ids, _handler_reads(cur) - before - overhead


def _record_locks_on_job(cur) -> list[dict]:
    cur.execute(
        "SELECT INDEX_NAME, LOCK_MODE, LOCK_DATA FROM performance_schema.data_locks "
        "WHERE OBJECT_SCHEMA = %s AND OBJECT_NAME = 'job' AND LOCK_TYPE = 'RECORD'",
        (TEST_DB,),
    )
    return cur.fetchall()


@pytest.fixture
def probe():
    conn = _test_connection(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT @@performance_schema AS on_")
        if not int(cur.fetchone()["on_"]):
            conn.close()
            pytest.skip("performance_schema is OFF")
    yield conn
    conn.close()


def test_one_wake_reads_and_locks_at_most_one_batch(probe):
    future, due = _seed()
    batch = future[:WAKE_BATCH]
    with probe.cursor() as cur:
        before = _schedule(cur)

    reader = _test_connection(autocommit=True)
    try:
        with reader.cursor() as cur:
            ids, reads = _measured_read(cur)
    finally:
        reader.close()
    assert ids == batch
    assert reads <= HANDLER_READS_MAX, reads  # work bound: the index prefix, not the table

    waker = _test_connection(autocommit=False)
    try:
        assert wake_auth_waiting_jobs(waker) == WAKE_BATCH
        with probe.cursor() as cur:
            locks = _record_locks_on_job(cur)
        # Lock bound. The UPDATE also rewrites the (status, scheduled_after) entries; InnoDB
        # keeps those as implicit locks, which data_locks shows only when another session
        # waits on one, so the bound is on PRIMARY.
        # A page-end `supremum pseudo-record` lock is no row, and no new job id (always the
        # highest) falls in a gap between two pages of old ids.
        primary = [lock for lock in locks
                   if lock["INDEX_NAME"] == "PRIMARY" and lock["LOCK_DATA"] != "supremum pseudo-record"]
        assert len(primary) <= WAKE_BATCH, len(primary)
        assert {int(lock["LOCK_DATA"]) for lock in primary} <= set(batch)  # no other row
        waker.commit()
    finally:
        waker.close()

    with probe.cursor() as cur:
        after = _schedule(cur)
        cur.execute("SELECT NOW() AS now")
        now = cur.fetchone()["now"]
    assert all(after[i] <= now for i in batch)
    assert all(after[i] > now for i in future[WAKE_BATCH:])
    changed = {i for i in before if after[i] != before[i]}
    assert changed == set(batch)  # no due, unrelated or non-waiting row moved
    assert set(due).isdisjoint(changed)


def test_a_wake_does_not_wait_for_rows_another_session_locks(probe):
    future, _due = _seed()
    batch = future[:WAKE_BATCH]
    holder = _test_connection(autocommit=False)
    reader = _test_connection(autocommit=True)
    try:
        with holder.cursor() as cur:
            cur.execute(
                f"SELECT id FROM job WHERE id IN ({', '.join(['%s'] * len(batch))}) FOR UPDATE",
                batch,
            )
        with reader.cursor() as cur:
            # A locking read would fail after 1 s here; the consistent read returns at once.
            cur.execute("SET SESSION innodb_lock_wait_timeout = 1")
            ids, reads = _measured_read(cur)
        assert ids == batch
        assert reads <= HANDLER_READS_MAX, reads
    finally:
        holder.rollback()
        holder.close()
        reader.close()


def test_two_concurrent_wakes_finish_without_deadlock(probe):
    future, _due = _seed()
    errors: list[BaseException] = []
    rounds = 20
    barrier = threading.Barrier(2)

    def wake() -> None:
        conn = _test_connection(autocommit=False)
        try:
            for _ in range(rounds):
                barrier.wait()
                try:
                    wake_auth_waiting_jobs(conn)
                    conn.commit()
                except BaseException as exc:  # a 1213 deadlock lands here
                    errors.append(exc)
                    conn.rollback()
        finally:
            conn.close()

    threads = [threading.Thread(target=wake) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    with probe.cursor() as cur:
        cur.execute("SELECT NOW() AS now")
        now = cur.fetchone()["now"]
        after = _schedule(cur)
    assert all(after[i] <= now for i in future[:WAKE_BATCH])
