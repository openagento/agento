"""The delta sink's rows and its caps (PRD E3-E5 §6.4.1, §8.2, §3.2).

The framework hands it a batch; everything below is the module's. Two rows per fragment in
one transaction: the `execution_delta` ledger, which is where the caps are counted and what
makes a re-delivery harmless, and the `conversation_event` a reader sees.
"""
from __future__ import annotations

import json
import uuid

import pytest

from agento.framework.execution_hooks import DeltaRecord
from agento.modules.conversation.src import service
from agento.modules.conversation.src.deltas import ConversationDeltaSink

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type  # noqa: F401


@pytest.fixture
def sink():
    instance = ConversationDeltaSink()
    yield instance
    if instance._conn is not None:
        instance._conn.close()


@pytest.fixture
def run(conn, world):
    """A conversation turn with a real execution, which is what a delta hangs off."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id=str(uuid.uuid4())[:16], content="pytanie")
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status) "
                    "VALUES (%s, %s, 1, 'running')", (execution_id, job_id))
    conn.commit()
    yield {"conversation_id": conversation_id, "message_id": message_id,
           "job_id": job_id, "execution_id": execution_id}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM execution_delta WHERE execution_id = %s", (execution_id,))
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s",
                    (conversation_id,))
        cur.execute("DELETE FROM execution WHERE execution_id = %s", (execution_id,))
    conn.commit()


def _delta(run, seq: int, text: str = "fragment", *, kind: str = "delta",
           tool_name: str | None = None) -> DeltaRecord:
    return DeltaRecord(execution_id=run["execution_id"], seq=seq, kind=kind,
                       text=text, tool_name=tool_name)


def _rows(conn, sql, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _ledger(conn, run) -> list[dict]:
    return _rows(conn, "SELECT * FROM execution_delta WHERE execution_id = %s ORDER BY seq",
                 (run["execution_id"],))


def _events(conn, run) -> list[dict]:
    return _rows(conn, "SELECT * FROM conversation_event WHERE conversation_id = %s "
                       "AND kind = 'assistant.delta' ORDER BY id", (run["conversation_id"],))


def _cap(monkeypatch, *, count=2000, size=1_048_576, page=100):
    values = {"stream/max_deltas_per_execution": count,
              "stream/max_delta_bytes_per_execution": size,
              "history/page_size": page}
    monkeypatch.setattr(service, "config", lambda conn, path: values[path])


# --- the two rows ----------------------------------------------------------

def test_one_fragment_writes_a_ledger_row_and_an_event(conn, sink, run, monkeypatch):
    _cap(monkeypatch)

    sink.write([_delta(run, 1, "cześć")])

    ledger = _ledger(conn, run)
    assert [(r["seq"], r["kind"]) for r in ledger] == [(1, "delta")]
    events = _events(conn, run)
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["text"] == "cześć"


def test_the_event_carries_the_sequence_and_the_tool_name(conn, sink, run, monkeypatch):
    _cap(monkeypatch)

    sink.write([_delta(run, 7, "ok", tool_name="bash")])

    payload = json.loads(_events(conn, run)[0]["payload"])
    assert (payload["seq"], payload["tool_name"], payload["fragment"]) == (7, "bash", "delta")


def test_a_batch_writes_every_fragment_in_order(conn, sink, run, monkeypatch):
    _cap(monkeypatch)

    sink.write([_delta(run, i, f"f{i}") for i in range(1, 6)])

    assert [r["seq"] for r in _ledger(conn, run)] == [1, 2, 3, 4, 5]
    assert [json.loads(e["payload"])["seq"] for e in _events(conn, run)] == [1, 2, 3, 4, 5]


def test_the_event_points_at_its_ledger_row(conn, sink, run, monkeypatch):
    """`(source_kind, source_id)` is unique, so the ledger's uniqueness carries into the
    stream: one fragment can never become two events."""
    _cap(monkeypatch)

    sink.write([_delta(run, 1)])

    ledger_id = _ledger(conn, run)[0]["id"]
    event = _events(conn, run)[0]
    assert (event["source_kind"], event["source_id"]) == ("delta", ledger_id)


def test_a_redelivered_fragment_writes_nothing_twice(conn, sink, run, monkeypatch):
    """The uniqueness IS the idempotency. A batch can be handed over twice after a retry."""
    _cap(monkeypatch)
    sink.write([_delta(run, 1, "once")])

    sink.write([_delta(run, 1, "once")])

    assert len(_ledger(conn, run)) == 1
    assert len(_events(conn, run)) == 1


def test_a_gap_marker_is_its_own_row(conn, sink, run, monkeypatch):
    _cap(monkeypatch)

    sink.write([_delta(run, 1), _delta(run, 2, kind="gap", text=None)])

    assert [r["kind"] for r in _ledger(conn, run)] == ["delta", "gap"]
    assert [json.loads(e["payload"])["fragment"] for e in _events(conn, run)] == \
        ["delta", "gap"]


def test_a_fragment_of_a_run_that_is_not_a_conversation_writes_no_event(conn, sink,
                                                                       monkeypatch):
    """The ledger row still stands: the cap is per execution, whoever owns it."""
    _cap(monkeypatch)
    orphan = str(uuid.uuid4())

    sink.write([DeltaRecord(execution_id=orphan, seq=1, kind="delta", text="x",
                            tool_name=None)])

    assert len(_rows(conn, "SELECT id FROM execution_delta WHERE execution_id = %s",
                     (orphan,))) == 1
    assert _rows(conn, "SELECT id FROM conversation_event WHERE execution_id = %s",
                 (orphan,)) == []
    with conn.cursor() as cur:
        cur.execute("DELETE FROM execution_delta WHERE execution_id = %s", (orphan,))
    conn.commit()


# --- the caps (§8.2) -------------------------------------------------------

def test_exceeding_the_count_cap_stops_deltas_and_records_one_marker(conn, sink, run,
                                                                     monkeypatch):
    _cap(monkeypatch, count=3)

    sink.write([_delta(run, i) for i in range(1, 11)])

    kinds = [r["kind"] for r in _ledger(conn, run)]
    assert kinds.count("delta") == 3
    assert kinds.count("truncated") == 1


def test_exceeding_the_byte_cap_stops_deltas_and_records_one_marker(conn, sink, run,
                                                                    monkeypatch):
    _cap(monkeypatch, size=30)

    sink.write([_delta(run, i, "x" * 20) for i in range(1, 6)])

    kinds = [r["kind"] for r in _ledger(conn, run)]
    assert kinds.count("delta") == 1        # 20 bytes fits, 40 does not
    assert kinds.count("truncated") == 1


def test_the_truncation_marker_is_recorded_once_across_batches(conn, sink, run, monkeypatch):
    _cap(monkeypatch, count=2)

    sink.write([_delta(run, i) for i in range(1, 6)])
    sink.write([_delta(run, i) for i in range(6, 11)])

    assert [r["kind"] for r in _ledger(conn, run)].count("truncated") == 1


def test_a_truncated_execution_writes_no_further_events(conn, sink, run, monkeypatch):
    _cap(monkeypatch, count=2)
    sink.write([_delta(run, i) for i in range(1, 6)])
    before = len(_events(conn, run))

    sink.write([_delta(run, i) for i in range(6, 20)])

    assert len(_events(conn, run)) == before


def test_a_gap_and_a_truncation_on_one_execution_are_two_distinct_events(conn, sink, run,
                                                                        monkeypatch):
    _cap(monkeypatch, count=2)

    sink.write([_delta(run, 1), _delta(run, 2, kind="gap", text=None),
                _delta(run, 3), _delta(run, 4), _delta(run, 5)])

    fragments = [json.loads(e["payload"])["fragment"] for e in _events(conn, run)]
    assert "gap" in fragments and "truncated" in fragments


def test_the_cap_is_resumed_from_the_database_by_a_fresh_sink(conn, run, monkeypatch):
    """A restart must not hand the execution a fresh allowance."""
    _cap(monkeypatch, count=3)
    first = ConversationDeltaSink()
    first.write([_delta(run, i) for i in range(1, 4)])
    first._conn.close()

    second = ConversationDeltaSink()
    try:
        second.write([_delta(run, i) for i in range(4, 8)])
    finally:
        second._conn.close()

    kinds = [r["kind"] for r in _ledger(conn, run)]
    assert kinds.count("delta") == 3 and kinds.count("truncated") == 1


# --- the connection --------------------------------------------------------

def test_the_sink_sets_a_statement_timeout(conn, sink, run, monkeypatch):
    """It is what bounds the framework's handover wait: the writer thread holds the handover
    lock across this whole call, so a `write` that could block for ever would make disabling
    the module block for ever too."""
    _cap(monkeypatch)
    sink.write([_delta(run, 1)])

    with sink._conn.cursor() as cur:
        cur.execute("SELECT @@SESSION.max_execution_time AS ms")
        ms = cur.fetchone()["ms"]

    assert ms > 0


# --- the byte cap is the SAME cap after a restart (CODE-8) -----------------

def test_the_byte_cap_measures_the_same_bytes_before_and_after_a_restart(conn, run,
                                                                        monkeypatch):
    """Live accounting counts the UTF-8 bytes of the text; the restart must count those.

    On the whole JSON payload's characters the cap changed size at every restart: the
    envelope's keys were counted, every escape was counted, and a multibyte character
    counted as one. The text here is multibyte on purpose - it is where the two
    measurements diverge most.
    """
    text = "zażółć"                              # 6 characters, 10 UTF-8 bytes
    _cap(monkeypatch, size=30)
    first = ConversationDeltaSink()
    first.write([_delta(run, i, text) for i in range(1, 3)])   # 20 bytes spent
    first._conn.close()

    second = ConversationDeltaSink()
    try:
        second.write([_delta(run, i, text) for i in range(3, 6)])
    finally:
        second._conn.close()

    kinds = [r["kind"] for r in _ledger(conn, run)]
    # 3 x 10 bytes fits under 30, the fourth does not - the same answer one sink would
    # have given without the restart in the middle.
    assert kinds.count("delta") == 3 and kinds.count("truncated") == 1


def test_the_in_memory_budget_map_is_bounded(conn, sink, run, monkeypatch):
    """One entry per execution, never removed, is unbounded memory in a consumer that runs
    for weeks. Eviction is safe because `_budget` reconstructs the spend from the two
    tables, so an evicted execution that speaks again resumes exactly where it was."""
    from agento.modules.conversation.src import deltas

    _cap(monkeypatch)
    monkeypatch.setattr(deltas, "_MAX_TRACKED_EXECUTIONS", 4)

    for n in range(20):
        sink.write([_delta({**run, "execution_id": f"e-{n}"}, 1)])

    assert len(sink._budgets) <= 4


def test_a_delta_that_arrives_before_the_mapping_is_created_on_redelivery(conn, run, sink,
                                                                         monkeypatch):
    """§4.1 writes `message.job_id` in a SECOND commit, so a delta can beat the mapping.

    The ledger row is written first and is immutable, so a re-delivery used to return at the
    ledger and the event could never be created. The retry has to be able to finish the work
    the first delivery could not.
    """
    _cap(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_id = NULL WHERE id = %s", (run["message_id"],))
    conn.commit()

    sink.write([_delta(run, 1, "early")])
    assert _events(conn, run) == []
    assert len(_ledger(conn, run)) == 1          # the ledger row stands

    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_id = %s WHERE id = %s",
                    (run["job_id"], run["message_id"]))
    conn.commit()

    sink.write([_delta(run, 1, "early")])        # the same fragment, re-delivered

    events = _events(conn, run)
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["text"] == "early"
    assert len(_ledger(conn, run)) == 1          # and still exactly one ledger row


def test_a_write_blocked_on_a_row_lock_gives_up_inside_the_timeout(conn, sink, run,
                                                                   monkeypatch):
    """`max_execution_time` bounds read-only SELECTs only, and every write here is an INSERT.

    The declared `core/sql_timeout_seconds` has to bound the WRITES too, or a row lock held
    by anyone parks the writer thread - and with it the handover lock, and with it disabling
    the module - for the server's 50-second lock-wait default or longer.
    """
    import time

    import pymysql

    from agento.modules.conversation.src import deltas as deltas_module

    from .conftest import _test_connection

    _cap(monkeypatch)
    monkeypatch.setattr(deltas_module, "_sql_timeout_seconds", lambda _conn: 1)
    sink.write([_delta(run, 1)])                       # opens the session with the timeout

    blocker = _test_connection()
    try:
        with blocker.cursor() as cur:
            cur.execute("INSERT INTO execution_delta (execution_id, seq, kind) "
                        "VALUES (%s, 2, 'delta')", (run["execution_id"],))
        started = time.monotonic()
        with pytest.raises(pymysql.err.OperationalError) as caught:
            sink.write([_delta(run, 2)])               # the same key: blocks on its lock
        elapsed = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert caught.value.args[0] == 1205                # lock wait timeout exceeded
    assert elapsed < 20                                # not the 50-second server default
