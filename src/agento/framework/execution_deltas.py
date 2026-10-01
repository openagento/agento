"""The bounded queue, the drain loop and the writer thread behind the delta seam (§6.4.1, §14).

**This file writes no row.** §14 puts the queue and the thread in the framework; §6.4.1 says
the framework writes no module table. Both hold because the thread owns the *mechanics* and
not the rows: it drains the queue, batches, and hands each batch to the registered
`ExecutionDeltaSink`. Every `INSERT` lives in the module that registered the sink.

Three properties are worth reading before changing anything here.

**Reuse is keyed on the declaration, never on the object.** `bootstrap()` re-imports and
re-instantiates every capability on each pass, and the consumer re-bootstraps every idle poll
tick. Keying on instance identity would stop and start this thread every five seconds for
ever. The key is `(module name, declared class path)` and the thread simply adopts the freshly
constructed instance.

**The handover lock spans the whole write, commit included.** The generation is ours, in
memory; the transaction is the sink's, and the sink commits it. A generation checked *before*
`sink.write(batch)` returns says nothing about the moment it commits - disablement could bump
it in between and the write would still land. So the thread holds `_handover_lock` around the
entire call, and a handover takes that same lock before bumping the generation. Either the
batch finished before the handover boundary, or the handover waits for it. There is no third
outcome. The lock is never held across a queue wait, so a producer is never blocked by it.

**A bounded `join()` is not proof the thread stopped**, so nothing here rests on it. A sink
can block inside the DB driver past the timeout; the sink's own statement timeout is what
bounds that. If a worker outlives its join, we start **no** replacement and say so: one stuck
thread that can no longer commit is safe, two live threads are not. The thread is a daemon, so
a stuck worker cannot hold the process up at shutdown either.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import replace

from .execution_hooks import DeltaRecord

logger = logging.getLogger(__name__)

# Deltas are declared lossy (§8.2). A full queue drops the OLDEST and marks the gap rather
# than blocking: the run is what matters, and a stalled drain thread is a stalled harness.
MAX_QUEUE = 2000
BATCH = 200
# And a bound on the MARKERS, which are one per losing execution: a stalled sink plus a
# flood of short-lived executions is an unbounded number of distinct ids, so "one marker
# per execution" is itself unbounded (CODE-8). Total pending is MAX_QUEUE + MAX_GAPS.
MAX_GAPS = 200
JOIN_TIMEOUT = 5.0

_state_lock = threading.Lock()
_handover_lock = threading.Lock()
_generation = 0
_worker: _Worker | None = None


class _Worker:
    def __init__(self, sink, key: tuple[str, str], generation: int) -> None:
        self.sink = sink
        self.key = key
        self.generation = generation
        self._queue: deque[DeltaRecord] = deque()
        # Gap markers live BESIDE the queue, one per execution, so a marker can neither be
        # dropped by a later overflow nor cost a slot the queue then has to pay back.
        self._gaps: dict[str, DeltaRecord] = {}
        self._cv = threading.Condition()
        self._stopping = False
        self.thread = threading.Thread(target=self._run, name="execution-deltas", daemon=True)

    # -- producer side (the harness drain thread) ---------------------------

    def offer(self, record: DeltaRecord) -> None:
        with self._cv:
            if len(self._queue) >= MAX_QUEUE:
                self._mark_gap(self._queue.popleft())
            self._queue.append(record)
            self._cv.notify()

    def _mark_gap(self, dropped: DeltaRecord) -> None:
        """One marker per losing execution, held BESIDE the queue, at most `MAX_GAPS` of them.

        Inside the queue a marker was wrong in both directions: put back without taking a
        record with it, it grew the queue by one on every overflow (CODE-8); taking one with
        it, the record taken could belong to ANOTHER execution, whose loss was then never
        announced. Beside the queue the marker costs no queue slot and cannot be dropped by a
        later overflow - but "one per execution" is only bounded while the executions are, so
        the map has its own ceiling. Full, the OLDEST marker goes: it belongs to the run that
        has been waiting longest to be told, which is the one most likely already over, and
        deltas are declared lossy (§8.2). The drain empties this map before the queue on every
        batch, so it only fills while the sink is stalled.
        """
        if dropped.execution_id in self._gaps:
            return
        if len(self._gaps) >= MAX_GAPS:
            self._gaps.pop(next(iter(self._gaps)))       # insertion-ordered: FIFO
        self._gaps[dropped.execution_id] = replace(
            dropped, kind="gap", text=None, tool_name=None)

    # -- consumer side (this thread) ----------------------------------------

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()

    def _take(self) -> list[DeltaRecord] | None:
        with self._cv:
            while not self._queue and not self._gaps and not self._stopping:
                self._cv.wait(0.2)
            if not self._queue and not self._gaps:
                return None
            # The markers first: each one says that what came before it is missing. At most
            # BATCH of them - one batch is one sink write, and it is bounded like any other.
            batch = [self._gaps.pop(key) for key in list(self._gaps)[:BATCH]]
            room = max(BATCH - len(batch), 0)
            batch += [self._queue.popleft() for _ in range(min(len(self._queue), room))]
            return batch

    def discard(self) -> None:
        with self._cv:
            self._queue.clear()
            self._gaps.clear()

    def pending(self) -> int:
        with self._cv:
            return len(self._queue) + len(self._gaps)

    def _run(self) -> None:
        while True:
            batch = self._take()
            if batch is None:
                return
            with _handover_lock:
                if self.generation != _generation:
                    # A handover completed while this batch waited. Writing it now would
                    # write a disabled module's table (MOD-2). Fail closed: §8.2 already
                    # declares deltas lossy, and a write after disablement is not undoable.
                    continue
                try:
                    self.sink.write(batch)
                except Exception as exc:
                    # Like `session_id_callback`: a sink that raises must not fail the run.
                    logger.warning("execution delta sink failed: %s", type(exc).__name__)


# -- the process-wide seam --------------------------------------------------

def submit(record: DeltaRecord) -> None:
    """Called on the harness's drain thread. Appends and returns - no DB work, ever."""
    worker = _worker
    if worker is None:
        # No sink registered: nothing is buffered, so nothing is dropped either.
        return
    worker.offer(record)


def is_streaming() -> bool:
    """Whether attaching a mapper to a run would lead anywhere."""
    return _worker is not None


def sync(sink, *, module: str | None = None, class_path: str | None = None) -> None:
    """Reconcile the thread to what `bootstrap()` just registered. Idempotent by design.

    `sink is None` means the module is off: stop, join, and **discard** the queue. Draining
    it into the old sink is exactly the MOD-2 leak this reconciliation exists to prevent.
    """
    global _worker
    with _state_lock:
        if sink is None:
            _stop_locked(discard=True)
            return
        key = (module or "", class_path or "")
        if _worker is not None and _worker.thread.is_alive() and _worker.key == key:
            # The same declaration, a new object: adopt it. Anything else would restart the
            # thread on every poll interval for ever.
            _worker.sink = sink
            return
        if _worker is not None and not _stop_locked(discard=True):
            logger.warning(
                "execution delta worker %s is still alive past its join; starting no "
                "replacement for %s", _worker.key, key)
            return
        _worker = _Worker(sink, key, _bump_generation())
        _worker.thread.start()


def shutdown() -> None:
    with _state_lock:
        _stop_locked(discard=False)


def _bump_generation() -> int:
    """Under the handover lock, so no in-flight write can straddle the boundary."""
    global _generation
    with _handover_lock:
        _generation += 1
        return _generation


def _stop_locked(*, discard: bool) -> bool:
    """True when the worker is really gone. Caller holds `_state_lock`."""
    global _worker
    worker = _worker
    if worker is None:
        return True
    if discard:
        # A handover or a replacement: nothing queued may be written any more, so the door
        # closes BEFORE the worker gets another chance at the sink (MOD-2).
        _bump_generation()
        worker.discard()
    worker.stop()
    worker.thread.join(JOIN_TIMEOUT)
    alive = worker.thread.is_alive()
    if not discard:
        # A process shutdown DRAINS: bumping the generation first made `_run` skip every
        # remaining batch, so the drain wrote nothing at all. The door closes after the
        # join, and it closes even when the worker outlived it.
        _bump_generation()
    if alive:
        return False
    _worker = None
    return True


def _reset_for_tests() -> None:
    global _worker, _generation
    with _state_lock:
        _stop_locked(discard=True)
        _worker = None
    _generation = 0
