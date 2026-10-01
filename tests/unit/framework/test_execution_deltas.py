"""The delta queue, the drain loop and the writer thread (PRD E3-E5 §14, §6.4.1).

The framework owns the mechanics and not the rows, so every sink here is a fake. What is
asserted is the contract the real sink relies on: the producer never blocks and never touches
a database, a full queue loses the oldest and SAYS so, and a handover cannot straddle a write.
"""
from __future__ import annotations

import threading
import time

import pytest

from agento.framework import execution_deltas
from agento.framework.execution_hooks import DeltaRecord


@pytest.fixture(autouse=True)
def _reset():
    execution_deltas._reset_for_tests()
    yield
    execution_deltas._reset_for_tests()


class FakeSink:
    def __init__(self) -> None:
        self.batches: list[list[DeltaRecord]] = []
        self.lock = threading.Lock()

    def write(self, batch) -> None:
        with self.lock:
            self.batches.append(list(batch))

    def records(self) -> list[DeltaRecord]:
        with self.lock:
            return [r for b in self.batches for r in b]


def _record(seq: int, *, execution_id: str = "e1", kind: str = "delta",
            text: str | None = "x") -> DeltaRecord:
    return DeltaRecord(execution_id=execution_id, seq=seq, kind=kind, text=text, tool_name=None)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _live() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "execution-deltas" and t.is_alive()]


# --- the producer side -----------------------------------------------------

def test_with_no_sink_nothing_is_buffered_and_no_thread_runs():
    """A disabled module streams nothing. Not "buffered and dropped later" - nothing."""
    execution_deltas.submit(_record(1))

    assert _live() == []
    assert execution_deltas.is_streaming() is False


def test_submit_does_no_database_work_on_the_calling_thread(monkeypatch):
    """The caller is the harness's stdout drain thread. A query here stalls the harness."""
    import agento.framework.db as db

    monkeypatch.setattr(db, "get_connection", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("the delta callback opened a connection")))
    sink = FakeSink()
    execution_deltas.sync(sink, module="m", class_path="src.deltas.Sink")

    for i in range(50):
        execution_deltas.submit(_record(i))

    assert _wait_for(lambda: len(sink.records()) == 50)


def test_submit_never_blocks_on_a_stalled_sink():
    """The queue is bounded and lossy on purpose: a stalled drain thread is a stalled run."""
    gate = threading.Event()

    class Stalled:
        def write(self, batch):
            gate.wait(10)

    execution_deltas.sync(Stalled(), module="m", class_path="p")
    started = time.monotonic()
    try:
        for i in range(execution_deltas.MAX_QUEUE + 500):
            execution_deltas.submit(_record(i))
    finally:
        gate.set()

    assert time.monotonic() - started < 5


def test_a_full_queue_drops_the_oldest_and_marks_the_gap():
    gate = threading.Event()
    sink = FakeSink()

    class Held:
        def write(self, batch):
            gate.wait(10)
            sink.write(batch)

    execution_deltas.sync(Held(), module="m", class_path="p")
    for i in range(execution_deltas.MAX_QUEUE + 100):
        execution_deltas.submit(_record(i))
    gate.set()

    assert _wait_for(lambda: any(r.kind == "gap" for r in sink.records()))
    kinds = [r.kind for r in sink.records()]
    assert kinds.count("gap") >= 1
    assert "delta" in kinds


def test_a_run_of_drops_marks_one_gap_not_one_per_dropped_record():
    """A marker per dropped record would be the flood it is meant to report."""
    worker = execution_deltas._Worker(FakeSink(), ("m", "p"), 1)
    for i in range(execution_deltas.MAX_QUEUE + 50):
        worker.offer(_record(i))

    # The markers live beside the queue and are sent FIRST, so this is where a run of drops
    # is counted - one marker for the execution, not one per dropped record.
    assert [r.kind for r in worker._gaps.values()] == ["gap"]
    assert worker._take()[0].kind == "gap"


def test_a_gap_and_a_truncation_on_one_execution_are_two_distinct_events():
    worker = execution_deltas._Worker(FakeSink(), ("m", "p"), 1)
    for i in range(execution_deltas.MAX_QUEUE + 10):
        worker.offer(_record(i))
    worker.offer(_record(9999, kind="truncated", text=None))

    kinds = [r.kind for r in worker._gaps.values()] + [r.kind for r in worker._queue]

    assert kinds.count("gap") == 1 and kinds.count("truncated") == 1


def test_two_executions_each_get_their_own_gap_marker():
    worker = execution_deltas._Worker(FakeSink(), ("m", "p"), 1)
    for i in range(execution_deltas.MAX_QUEUE):
        worker.offer(_record(i, execution_id="e1"))
    worker.offer(_record(0, execution_id="e2"))
    worker.offer(_record(1, execution_id="e2"))

    assert set(worker._gaps) == {"e1"}     # only e1 lost anything; e2's records all fit


# --- the sink is not allowed to break the run ------------------------------

def test_a_sink_that_raises_does_not_kill_the_thread_or_the_run():
    calls = []

    class Angry:
        def write(self, batch):
            calls.append(batch)
            raise RuntimeError("the database went away")

    execution_deltas.sync(Angry(), module="m", class_path="p")
    execution_deltas.submit(_record(1))

    assert _wait_for(lambda: len(calls) >= 1)
    execution_deltas.submit(_record(2))
    assert _wait_for(lambda: len(calls) >= 2)      # still draining
    assert len(_live()) == 1


# --- the thread lifecycle (MOD-2) ------------------------------------------

def test_two_syncs_of_one_declaration_leave_the_same_thread_running():
    """Asserted on IDENTITY: `bootstrap()` builds a new sink object every poll tick, and a
    thread keyed on the object would restart every five seconds for ever."""
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")
    first = _live()[0]

    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")

    assert _live() == [first]


def test_the_thread_adopts_the_freshly_constructed_sink():
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")
    second = FakeSink()
    execution_deltas.sync(second, module="conversation", class_path="src.deltas.Sink")

    execution_deltas.submit(_record(1))

    assert _wait_for(lambda: len(second.records()) == 1)


def test_ten_consecutive_syncs_neither_start_nor_stop_a_thread():
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")
    first = _live()[0]

    for _ in range(10):
        execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")

    assert _live() == [first]


def test_a_different_declaration_stops_the_first_and_leaves_exactly_one():
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.A")
    first = _live()[0]
    second_sink = FakeSink()

    execution_deltas.sync(second_sink, module="conversation", class_path="src.deltas.B")

    live = _live()
    assert len(live) == 1 and live[0] is not first
    assert _wait_for(lambda: not first.is_alive())
    execution_deltas.submit(_record(1))
    assert _wait_for(lambda: len(second_sink.records()) == 1)


def test_disabling_the_module_joins_the_thread_and_writes_nothing_after():
    sink = FakeSink()
    execution_deltas.sync(sink, module="conversation", class_path="src.deltas.Sink")
    execution_deltas.submit(_record(1))
    assert _wait_for(lambda: len(sink.records()) == 1)

    execution_deltas.sync(None)

    assert _live() == []
    execution_deltas.submit(_record(2))
    time.sleep(0.2)
    assert [r.seq for r in sink.records()] == [1]


def test_disabling_discards_the_pending_queue_rather_than_draining_it():
    """Draining into the old sink after the module is off is the MOD-2 leak itself."""
    gate = threading.Event()
    sink = FakeSink()

    class Held:
        def write(self, batch):
            gate.wait(10)
            sink.write(batch)

    execution_deltas.sync(Held(), module="conversation", class_path="src.deltas.Sink")
    for i in range(100):
        execution_deltas.submit(_record(i))
    gate.set()

    execution_deltas.sync(None)
    time.sleep(0.2)

    assert len(sink.records()) < 100


def test_re_enabling_starts_a_fresh_thread_and_streams_again():
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")
    execution_deltas.sync(None)
    assert _live() == []

    revived = FakeSink()
    execution_deltas.sync(revived, module="conversation", class_path="src.deltas.Sink")
    execution_deltas.submit(_record(1))

    assert _wait_for(lambda: len(revived.records()) == 1)
    assert len(_live()) == 1


def test_a_worker_that_outlives_its_join_starts_no_second_thread(monkeypatch):
    """The bounded join cannot prove the thread stopped, so the guarantee does not rest on
    it: enumerate the live threads instead of trusting the join's return. One stuck worker
    that can no longer commit is safe; two live workers are not.

    The stop signal is suppressed here to make the join time out on demand. In production
    this branch is reached only by a worker blocked inside `write` - and that one holds the
    handover lock, which the next test is about.
    """
    monkeypatch.setattr(execution_deltas, "JOIN_TIMEOUT", 0.05)
    monkeypatch.setattr(execution_deltas._Worker, "stop", lambda self: None)
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.A")
    stuck = _live()[0]

    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.B")

    assert _live() == [stuck]              # no replacement while the first is alive


def test_a_write_blocked_past_the_join_blocks_the_handover_rather_than_racing_it(monkeypatch):
    """There is no "the thread is stuck inside write and the handover went ahead" state: the
    write holds the handover lock, so the handover waits for it. That is the invariant -
    no write after the boundary - and it does not depend on the join at all."""
    monkeypatch.setattr(execution_deltas, "JOIN_TIMEOUT", 0.05)
    inside, release = threading.Event(), threading.Event()

    class Blocked:
        def write(self, batch):
            inside.set()
            release.wait(10)

    execution_deltas.sync(Blocked(), module="conversation", class_path="src.deltas.A")
    execution_deltas.submit(_record(1))
    assert inside.wait(5)

    done = threading.Event()
    threading.Thread(target=lambda: (execution_deltas.sync(None), done.set()),
                     daemon=True).start()
    try:
        assert not done.wait(0.5)          # blocked on the lock, not racing the commit
    finally:
        release.set()
    assert done.wait(5)
    assert _live() == []


# --- the commit race the handover lock exists for --------------------------

def test_a_disable_cannot_complete_while_a_write_is_between_insert_and_commit():
    """The generation is ours and in memory; the transaction is the sink's and the sink
    commits it. A check before `write` returns says nothing about the moment it commits, so
    the lock spans the whole call - and this is what that buys."""
    inserted, release = threading.Event(), threading.Event()
    committed = []

    class SlowCommit:
        def write(self, batch):
            inserted.set()                  # INSERT done, transaction still open
            release.wait(10)
            committed.append(list(batch))   # COMMIT

    execution_deltas.sync(SlowCommit(), module="conversation", class_path="src.deltas.Sink")
    execution_deltas.submit(_record(1))
    assert inserted.wait(5)

    done = threading.Event()
    threading.Thread(target=lambda: (execution_deltas.sync(None), done.set()),
                     daemon=True).start()

    # The whole point of the lock: the disable is BLOCKED, not racing the commit.
    assert not done.wait(0.5)
    assert committed == []

    release.set()
    assert done.wait(5)
    assert len(committed) == 1              # it finished before the handover boundary


def test_nothing_commits_after_a_disable_reports_done():
    committed = []

    class Recording:
        def write(self, batch):
            time.sleep(0.05)
            committed.append(list(batch))

    execution_deltas.sync(Recording(), module="conversation", class_path="src.deltas.Sink")
    for i in range(50):
        execution_deltas.submit(_record(i))

    execution_deltas.sync(None)
    settled = len(committed)
    time.sleep(0.3)

    assert len(committed) == settled


def test_a_batch_queued_before_a_completed_handover_is_dropped_not_written():
    """Fail closed. §8.2 already declares deltas lossy; a write into a disabled module's
    table is not undoable."""
    sink = FakeSink()
    worker = execution_deltas._Worker(sink, ("conversation", "p"), generation=-1)
    worker.offer(_record(1))
    worker.stop()

    worker._run()

    assert sink.records() == []


# --- shutdown --------------------------------------------------------------

def test_shutdown_stops_the_thread():
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")

    execution_deltas.shutdown()

    assert _live() == []


def test_the_worker_thread_is_a_daemon():
    """A stuck worker must not keep the process up at SIGTERM."""
    execution_deltas.sync(FakeSink(), module="conversation", class_path="src.deltas.Sink")

    assert _live()[0].daemon is True


def test_the_queue_stays_bounded_under_sustained_overflow():
    """CODE-8. The gap marker has to CONSUME a slot, not add one.

    Putting the marker back without taking a record with it grew the queue by one on every
    overflow: 12,000 records offered into a 2,000-slot queue left 12,000 of them in memory,
    which is the unbounded growth the cap exists to prevent.
    """
    worker = execution_deltas._Worker(FakeSink(), ("m", "p"), 1)

    for i in range(execution_deltas.MAX_QUEUE * 6):
        worker.offer(_record(i))

    assert len(worker._queue) == execution_deltas.MAX_QUEUE
    assert set(worker._gaps) == {"e1"}     # the marker, beside the queue, costs no slot


def test_shutdown_drains_what_is_queued_through_the_live_sink():
    """A process shutdown DRAINS; only a handover discards.

    Advancing the generation before the join made `_run` skip every remaining batch, so the
    drain the consumer's shutdown path exists for wrote nothing at all.
    """
    gate = threading.Event()
    sink = FakeSink()

    class Held:
        def write(self, batch):
            gate.wait(10)
            sink.write(batch)

    execution_deltas.sync(Held(), module="conversation", class_path="src.deltas.Sink")
    execution_deltas.submit(_record(1))
    execution_deltas.submit(_record(2))
    gate.set()

    execution_deltas.shutdown()

    assert [r.seq for r in sink.records()] == [1, 2]
    assert _live() == []


def test_every_execution_that_loses_a_record_gets_its_own_marker():
    """The queue is shared, the loss is not. Dropping a record to pay for another
    execution's marker lost that execution's fragments with nothing to say so."""
    worker = execution_deltas._Worker(FakeSink(), ("m", "p"), 1)

    for i in range(execution_deltas.MAX_QUEUE):
        worker.offer(_record(i, execution_id="e1"))
    for i in range(execution_deltas.MAX_QUEUE * 2):
        worker.offer(_record(i, execution_id="e2"))

    assert set(worker._gaps) == {"e1", "e2"}
    assert len(worker._queue) == execution_deltas.MAX_QUEUE


def test_a_flood_of_distinct_executions_cannot_grow_the_pending_state(monkeypatch):
    """CODE-8. Moving the markers beside the queue bounded the queue and unbounded the map:
    "one marker per execution" is one per DISTINCT id, and a stalled sink plus short-lived
    executions has no bound on those. Total pending state, and one batch, are both capped."""
    monkeypatch.setattr(execution_deltas, "MAX_QUEUE", 20)
    monkeypatch.setattr(execution_deltas, "MAX_GAPS", 5)
    monkeypatch.setattr(execution_deltas, "BATCH", 4)
    worker = execution_deltas._Worker(sink=None, key=("m", "c"), generation=0)

    for i in range(600):                       # every record a different execution
        worker.offer(_record(i, execution_id=f"e{i}"))

    assert worker.pending() <= 20 + 5
    batch = worker._take()
    assert batch is not None
    assert len(batch) <= 4
    assert sum(1 for r in batch if r.kind == "gap") <= 5
