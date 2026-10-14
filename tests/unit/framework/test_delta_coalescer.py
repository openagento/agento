"""Live text: the per-execution coalescer and the paced writer (E9 chat UX, plan B2).

The worker is driven on the test thread with an injected clock: `_take` returns at once when
something is due, so each assertion is about one batching decision, not about timing.
"""
from __future__ import annotations

import itertools
import threading

import pytest

from agento.framework import execution_deltas as ed
from agento.framework.execution_hooks import DeltaRecord

SECRET = "cap_0123456789abcdef"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def worker():
    w = ed._Worker(sink=None, key=("m", "c"), generation=1)
    w._clock = Clock()
    w.open("e1", itertools.count(1), (SECRET,))
    return w


def _take(w, advance: float = 0.0):
    w._clock.now += advance
    w._next_write_at = 0.0          # the pace already allows a write
    return w._take()


def _complete(kind: str, text: str = "done", seq: int = 1) -> DeltaRecord:
    return DeltaRecord(execution_id="e1", seq=seq, kind=kind, text=text, tool_name=None)


def _rows(batch):
    return [(r.kind, r.text) for r in batch or []]


def test_partials_join_into_one_row_after_silence(worker):
    for piece in ("Hel", "lo ", "world"):
        worker.offer_partial("e1", "assistant.partial", piece)

    assert worker._queue == ed.deque()               # not due yet: < PARTIAL_FLUSH_MS
    assert _rows(_take(worker, 0.3)) == [("assistant.partial", "Hello world")]


def test_a_4k_buffer_flushes_before_its_time(worker):
    worker.offer_partial("e1", "assistant.partial", "x" * ed.PARTIAL_FLUSH_BYTES)

    assert _rows(_take(worker, 0.0)) == [("assistant.partial", "x" * ed.PARTIAL_FLUSH_BYTES)]


def test_a_kind_change_queues_the_open_segment_first(worker):
    worker.offer_partial("e1", "reasoning.partial", "hmm")
    worker.offer_partial("e1", "assistant.partial", "Hi")

    assert _rows(_take(worker, 0.3)) == [("reasoning.partial", "hmm"), ("assistant.partial", "Hi")]


def test_the_complete_fragment_supersedes_its_segment(worker):
    worker.offer_partial("e1", "assistant.partial", "Hel")
    worker.offer(_complete("assistant.text", "Hello"))

    assert _rows(_take(worker, 0.3)) == [("assistant.text", "Hello")]


def test_a_complete_of_another_kind_keeps_order_and_seq(worker):
    worker.offer_partial("e1", "reasoning.partial", "think")
    worker.offer(_complete("tool.started", None, seq=1))

    batch = _take(worker, 0.3)
    assert _rows(batch) == [("reasoning.partial", "think"), ("tool.started", None)]
    assert [r.seq for r in batch] == sorted(r.seq for r in batch)
    assert len({r.seq for r in batch}) == 2


def test_close_writes_the_tail(worker):
    worker.offer_partial("e1", "assistant.partial", "cut off")
    worker.close("e1")

    assert _rows(_take(worker)) == [("assistant.partial", "cut off")]
    assert "e1" not in worker._pending


def test_a_secret_split_across_input_pieces_is_redacted(worker):
    worker.offer_partial("e1", "assistant.partial", "token " + SECRET[:7])
    worker.offer_partial("e1", "assistant.partial", SECRET[7:] + " end")

    assert _rows(_take(worker, 0.3)) == [("assistant.partial", "token *** end")]


def test_a_secret_split_across_a_timed_flush_is_held_back(worker):
    worker.offer_partial("e1", "assistant.partial", "token " + SECRET[:7])
    first = _take(worker, 0.3)
    worker.offer_partial("e1", "assistant.partial", SECRET[7:] + " end")
    second = _take(worker, 0.3)

    written = "".join(r.text for r in first + second)
    assert SECRET[:7] not in written and SECRET not in written
    assert written == "token *** end"


def test_a_secret_split_across_close_is_redacted(worker):
    worker.offer_partial("e1", "assistant.partial", "a " + SECRET[:5])
    first = _take(worker, 0.3)
    worker.offer_partial("e1", "assistant.partial", SECRET[5:])
    worker.close("e1")

    assert "".join(r.text for r in first + _take(worker)) == "a ***"


def test_pacing_holds_a_second_write_until_the_interval(worker):
    worker.offer(_complete("tool.started", None))
    assert _rows(_take(worker)) == [("tool.started", None)]
    worker.offer(_complete("tool.completed", None, seq=2))

    # The write just happened: the next one waits for WRITE_INTERVAL_S.
    got = []
    t = threading.Thread(target=lambda: got.append(worker._take()))
    t.start()
    t.join(0.1)
    assert t.is_alive()                              # still waiting on the pace
    worker._clock.now += ed.WRITE_INTERVAL_S
    with worker._cv:
        worker._cv.notify()
    t.join(2)
    assert _rows(got[0]) == [("tool.completed", None)]


def test_a_full_batch_goes_at_once(worker):
    worker.offer(_complete("tool.started", None))
    _take(worker)
    for i in range(ed.BATCH):
        worker.offer(_complete("tool.started", None, seq=10 + i))

    assert len(worker._take()) == ed.BATCH            # no wait for the pace


def test_the_buffer_is_bounded_while_the_sink_is_blocked(worker):
    """1 MiB of live text and no write: memory stays bounded, no secret, no gap."""
    chunk = ("y" * 1000 + SECRET[:4]) + (SECRET[4:] + "z" * 1000)
    for _ in range(1024 * 1024 // len(chunk)):
        worker.offer_partial("e1", "assistant.partial", chunk)
        pending = worker._pending["e1"]
        assert pending.size <= ed.PARTIAL_MAX_BYTES + len(chunk.encode())
    assert len(worker._queue) <= ed.MAX_QUEUE
    assert worker._gaps == {}
    assert all(SECRET not in r.text for r in worker._queue)
    worker.offer(_complete("assistant.text", "final"))
    assert worker._queue[-1].kind == "assistant.text"


def test_a_full_queue_drops_partials_silently(worker, monkeypatch):
    monkeypatch.setattr(ed, "MAX_QUEUE", 3)
    for i in range(3):
        worker.offer(_complete("tool.started", None, seq=i + 1))
    worker.offer_partial("e1", "assistant.partial", "x" * ed.PARTIAL_MAX_BYTES)

    assert [r.kind for r in worker._queue] == ["tool.started"] * 3
    assert worker._gaps == {}


def test_discard_drops_pending_partials(worker):
    worker.offer_partial("e1", "assistant.partial", "x")
    worker.discard()

    assert worker._pending == {}


def test_an_unopened_execution_streams_no_partials():
    w = ed._Worker(sink=None, key=("m", "c"), generation=1)
    w.offer_partial("e9", "assistant.partial", "x")

    assert w._pending == {} and not w._queue


@pytest.mark.parametrize("boundary", ["tool", "kind", "close"])
def test_a_boundary_never_writes_a_secret_prefix(worker, boundary):
    """Review impl-1 F1 (SEC-6): a forced flush once wrote its held-back tail, so `cap_0123`
    + a tool row + `456789abcdef` put the whole token on screen when a reader joined them."""
    worker.offer_partial("e1", "assistant.partial", "see " + SECRET[:8])
    if boundary == "tool":
        worker.offer(_complete("tool.started", None, seq=1))
    elif boundary == "kind":
        worker.offer_partial("e1", "reasoning.partial", "x")
    else:
        worker.close("e1")
    if boundary == "close":
        worker.open("e1", itertools.count(10), (SECRET,))      # the next run of the thread
    worker.offer_partial("e1", "assistant.partial", SECRET[8:])
    worker.close("e1")

    joined = "".join(r.text or "" for r in _take(worker, 0.3))
    assert SECRET not in joined and SECRET[:8] not in joined
    assert joined.startswith("see ")


def test_one_huge_partial_is_cut_to_the_record_bound(worker):
    """Review impl-1 F2 (CODE-8): one 1 MiB piece was queued whole."""
    worker.offer_partial("e1", "assistant.partial", "x" * (1024 * 1024))

    queued = sum(len(r.text.encode()) for r in worker._queue)
    pending = worker._pending["e1"].size
    assert queued + pending <= ed.PARTIAL_MAX_BYTES


def test_full_batches_do_not_postpone_a_quiet_runs_partial(worker):
    """Review impl-1 F3: due text was flushed only once the pace allowed a write, and every
    full batch moved that deadline, so a busy writer starved a quiet run's live text."""
    worker.offer_partial("e1", "assistant.partial", "quiet")
    seen = []
    for step in range(12):                           # 1.2 s of full batches, one per 100 ms
        worker._clock.now += 0.1
        worker._next_write_at = worker._clock.now + 1   # the pace never allows a timed write
        for i in range(ed.BATCH):
            worker.offer(DeltaRecord(execution_id="e2", seq=step * ed.BATCH + i,
                                     kind="tool.started", text=None, tool_name=None))
        seen += _rows(worker._take())
        if ("assistant.partial", "quiet") in seen:
            break

    assert ("assistant.partial", "quiet") in seen and step <= 5
