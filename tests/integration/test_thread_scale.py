"""The claim's thread steps at `CONCURRENT_WORKERS_STRESS_TEST` claimants (SCL-1).

Separate threads never wait for each other, and one thread never runs two turns at once.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

from agento.framework.consumer import Consumer
from agento.framework.job_models import AgentType
from agento.framework.publisher import insert_job

from .conftest import CONCURRENT_WORKERS_STRESS_TEST, DB_STRESS, _test_connection

pytestmark = DB_STRESS
N = CONCURRENT_WORKERS_STRESS_TEST
DEADLINE_S = 120.0


@pytest.fixture
def view() -> int:
    c = _test_connection(autocommit=True)
    with c.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('ts_ws', 't')")
        cur.execute("SELECT id FROM workspace WHERE code = 'ts_ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'ts_av', 't')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'ts_av'")
        view_id = cur.fetchone()["id"]
    c.close()
    return view_id


_batches = iter(range(10**6))


def _insert(view: int, references: list[str]) -> list[int]:
    batch = next(_batches)
    c = _test_connection()
    try:
        with c.cursor() as cur:
            ids = [insert_job(cur, agent_type=AgentType.TODO, source="jira", agent_view_id=view,
                              reference_id=ref, idempotency_key=f"s:{batch}:{i}", priority=50,
                              max_attempts=1, requester=None)
                   for i, ref in enumerate(references)]
        c.commit()
    finally:
        c.close()
    return ids


def _sql(sql: str, params=()) -> list[dict]:
    c = _test_connection(autocommit=True)
    with c.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    c.close()
    return rows


def _claimants(int_db_config, int_consumer_config, work) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(N)

    def run() -> None:
        consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("scale"))
        barrier.wait()
        try:
            work(consumer)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(N)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=DEADLINE_S + 30)
    return errors


def test_jobs_in_separate_threads_are_all_claimed_at_once(
    int_db_config, int_consumer_config, view, tmp_path, monkeypatch,
):
    from agento.framework import artifacts_dir
    monkeypatch.setattr(artifacts_dir, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    _insert(view, [f"AI-{i}" for i in range(N)])

    def claim_and_prepare(consumer: Consumer) -> None:
        job = consumer._try_dequeue()
        run = artifacts_dir.build_artifacts_dir("ts_ws", "ts_av", job.id)
        artifacts_dir.prepare_artifacts_dir(run)
        artifacts_dir.prepare_thread_dir(job.thread_key, run)  # N at once: idempotent, no race

    errors = _claimants(int_db_config, int_consumer_config, claim_and_prepare)

    assert not errors
    assert _sql("SELECT COUNT(*) AS n FROM thread")[0]["n"] == N
    keys = {r["key"] for r in _sql("SELECT `key` FROM thread")}
    assert {p.name for p in (tmp_path / "artifacts" / "_threads").iterdir()} == keys
    assert _sql("SELECT COUNT(*) AS n FROM job WHERE status = 'RUNNING'")[0]["n"] == N
    assert _sql("SELECT COUNT(*) AS n FROM job_defer_stretch")[0]["n"] == 0


def test_jobs_of_one_thread_run_one_at_a_time_and_all_finish(
    int_db_config, int_consumer_config, view, monkeypatch,
):
    # A deferred turn is offered again at once, so the claimants race for the line. The
    # claims of one thread take its row lock one at a time (`threads.lock_thread`), so N
    # claimants share fewer turns than N: the race, not the throughput of one thread.
    monkeypatch.setattr("agento.framework.consumer.defer_seconds", lambda _ms: 0)
    turns = 20
    ids = _insert(view, ["AI-1"] * turns)
    lock = threading.Lock()
    peak = 0
    done: list[int] = []
    deadline = time.monotonic() + DEADLINE_S

    def drain(consumer: Consumer) -> None:
        nonlocal peak
        while len(done) < turns and time.monotonic() < deadline:
            job = consumer._try_dequeue()
            if job is None:
                continue
            with lock:
                running = _sql("SELECT COUNT(*) AS n FROM job WHERE status = 'RUNNING'")[0]["n"]
                peak = max(peak, running)
                done.append(job.id)
                _sql("UPDATE job SET status = 'SUCCESS', session_id = %s WHERE id = %s",
                     (f"s-{job.id}", job.id))

    errors = _claimants(int_db_config, int_consumer_config, drain)

    assert not errors
    assert peak == 1
    assert done == ids                                   # FIFO along the line
    rows = _sql("SELECT id, resume_job_id FROM job ORDER BY id")
    assert [r["resume_job_id"] for r in rows] == [None, *ids[:-1]]   # one linear session


def test_waiting_turns_do_not_hold_a_free_thread_and_back_off(
    int_db_config, int_consumer_config, view, monkeypatch,
):
    """N turns wait behind one RUNNING turn; a job of another thread queued after all of
    them is claimed in one sweep, and the waiting turns are not offered again at once."""
    from agento.framework.threads import line_defer_ms

    # The real back-off, ten times longer: a sweep slower than the first deferral (a loaded
    # test host) would offer a waiting turn again and make the pass count a timing.
    monkeypatch.setattr("agento.framework.consumer.line_defer_ms", lambda n: 10 * line_defer_ms(n))
    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("scale"))
    [running] = _insert(view, ["AI-1"])
    assert consumer._try_dequeue().id == running
    _insert(view, ["AI-1"] * N)
    [free] = _insert(view, ["AI-2"])

    passes = 0
    while (job := consumer._try_dequeue()) is None and passes <= 2 * N:
        assert consumer._claim_deferred
        passes += 1

    assert job is not None and job.id == free and passes == N
    assert _sql("SELECT MAX(defer_count) AS n FROM job_defer_stretch")[0]["n"] == 1
    # The next deferral of a waiting turn is longer: the stretch counts it (line_defer_ms).
    _sql("UPDATE job SET scheduled_after = NOW() WHERE status = 'TODO'")
    consumer._try_dequeue()
    [waited] = _sql("SELECT TIMESTAMPDIFF(SECOND, NOW(), scheduled_after) AS s FROM job "
                    "WHERE id = %s", (running + 1,))
    # The second deferral, not the first again: TIMESTAMPDIFF floors, so one second less.
    assert waited["s"] >= 10 * line_defer_ms(2) // 1000 - 1


def test_the_resume_target_is_read_with_a_backward_index_scan(int_db_config, view):
    """CODE-8: the latest finished turn is found without reading the thread's history."""
    from agento.framework.job_models import Job
    from agento.framework.threads import line_blocked, resolve_resume

    history = _insert(view, ["AI-1"] * 2000)                       # a long, old thread
    _insert(view, [f"AI-{i % 500 + 2}" for i in range(5000)])     # newer jobs of other threads
    [turn] = _insert(view, ["AI-1"])
    _sql("UPDATE job SET status = 'SUCCESS', session_id = CONCAT('s-', id), "
         "finished_at = NOW() - INTERVAL (%s - id) SECOND WHERE id < %s", (turn, turn))
    _sql("ANALYZE TABLE job")

    c = _test_connection(autocommit=True)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM job WHERE id = %s", (turn,))
            job = Job.from_row(cur.fetchone())
            reads = {}
            for name, query in (("resume", resolve_resume), ("blocked", line_blocked)):
                cur.execute("FLUSH STATUS")
                reads[f"{name}_result"] = query(cur, job)
                cur.execute("SHOW SESSION STATUS LIKE 'Handler_read%%'")
                reads[name] = sum(int(r["Value"]) for r in cur.fetchall())
    finally:
        c.close()

    assert (reads["resume_result"], reads["blocked_result"]) == (history[-1], False)
    # Not the 2000 turns of history, not the 5000 jobs of other threads. `line_blocked`
    # reads one index range per open status and clause (about 25 dives), never the rows.
    assert reads["resume"] < 100 and reads["blocked"] < 100, reads


def test_children_of_many_roots_ending_at_once_wake_each_root_once(view, monkeypatch):
    """N roots, each with 5 children; the 5N children end at once across N threads (each
    thread ends children of 5 different roots, so siblings end side by side). Exactly N
    wakes, no deadlock, and no observer call reads more than a fixed number of times."""
    from pymysql.cursors import DictCursor

    from agento.framework.events import JobSucceededEvent
    from agento.framework.job_models import Job
    from agento.modules.subagent.src.observers import WakeParentObserver

    roots = _insert(view, [f"AI-{i}" for i in range(N)])
    _sql("UPDATE job SET status = 'SUCCESS'")
    c = _test_connection()
    try:
        with c.cursor() as cur:
            cur.execute(f"SELECT id, thread_id FROM job WHERE id IN ({','.join(['%s'] * N)})", roots)
            threads = {r["id"]: r["thread_id"] for r in cur.fetchall()}
            children = [insert_job(cur, agent_type=AgentType.FOLLOWUP, source="subagent",
                                   agent_view_id=view, reference_id=str(root),
                                   idempotency_key=f"kid:{root}:{k}", priority=50, max_attempts=1,
                                   requester=None, thread_id=threads[root], parent_job_id=root,
                                   depth=1)
                        for k in range(5) for root in roots]      # interleaved by root
        c.commit()
    finally:
        c.close()
    _sql("UPDATE job SET status = 'RUNNING' WHERE parent_job_id IS NOT NULL")

    reads = threading.local()
    real = DictCursor.execute

    def counted(self, query, args=None):
        reads.n = getattr(reads, "n", 0) + 1
        return real(self, query, args)

    monkeypatch.setattr(DictCursor, "execute", counted)
    per_call: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(N)

    def finish(mine: list[int]) -> None:
        barrier.wait()
        try:
            for job_id in mine:
                _sql("UPDATE job SET status = 'SUCCESS' WHERE id = %s", (job_id,))
                job = Job.from_row(_sql("SELECT * FROM job WHERE id = %s", (job_id,))[0])
                reads.n = 0
                WakeParentObserver().execute(JobSucceededEvent(job=job))
                per_call.append(reads.n)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=finish, args=(children[i * 5:(i + 1) * 5],))
               for i in range(N)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=DEADLINE_S)

    assert not errors and not any(w.is_alive() for w in workers)
    wakes = _sql("SELECT idempotency_key FROM job WHERE idempotency_key LIKE 'subagent\\_wake:%%'")
    assert sorted(w["idempotency_key"] for w in wakes) == sorted(f"subagent_wake:{r}" for r in roots)
    # Per end: the ancestor read, then `maybe_wake` for the row and its root (the levels, the
    # root, the wake keys, one config read and, once per root, the publish). Not a read per
    # sibling.
    assert max(per_call) <= 20, max(per_call)
