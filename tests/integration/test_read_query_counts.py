"""One guard for the whole class: a read whose query count grows with the item count (CODE-8).

Every loop here is bounded - a history page, a 500-row relay batch, one delta batch - so the
defect is not unbounded growth but per-item work a single batch query already does. The
shape banned is structural: run the same operation over N items and over 3N, and the number
of SELECTs must not move.
"""
from __future__ import annotations

import uuid

import pymysql
import pytest

from agento.framework.execution_hooks import DeltaRecord
from agento.framework.outbox import write_outbox
from agento.modules.conversation.src import relay, service
from agento.modules.conversation.src.deltas import ConversationDeltaSink

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type  # noqa: F401


@pytest.fixture
def selects(monkeypatch):
    """Every SELECT any connection runs while this is installed."""
    seen: list[str] = []
    real = pymysql.cursors.DictCursor.execute

    def spy(self, query, args=None):
        if query.lstrip().upper().startswith("SELECT"):
            seen.append(query)
        return real(self, query, args)

    monkeypatch.setattr(pymysql.cursors.DictCursor, "execute", spy)
    return seen


def _threads(conn, world, n: int) -> None:
    for i in range(n):
        service.create_conversation(conn, user_id=world["owner"].id,
                                    agent_view_id=world["view"], title=f"t{i}")


def test_a_history_page_costs_the_same_reads_whatever_its_length(conn, world, selects):
    """The reach gate is per agent_view, not per row: one page of one user's threads spans
    a handful of views, and asking the same question once per row is the defect."""
    _threads(conn, world, 2)
    selects.clear()
    service.list_visible(conn, user=world["owner"], limit=50)
    few = len(selects)

    _threads(conn, world, 6)
    selects.clear()
    service.list_visible(conn, user=world["owner"], limit=50)

    assert len(selects) == few


def _outbox_rows(conn, world, n: int) -> None:
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _message_id, job_id, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["owner"].id,
        client_message_id=str(uuid.uuid4())[:16], content="p")
    with conn.cursor() as cur:
        for i in range(n):
            write_outbox(cur, job_id=job_id, kind=f"job.step{i}", payload={})
    conn.commit()


def test_a_relay_batch_resolves_its_messages_in_one_query(conn, world, selects):
    _outbox_rows(conn, world, 2)
    selects.clear()
    relay.relay_outbox(conn)
    few = len(selects)

    _outbox_rows(conn, world, 6)
    selects.clear()
    relay.relay_outbox(conn)

    assert len(selects) == few


@pytest.fixture
def run(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    _, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id=str(uuid.uuid4())[:16], content="p")
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status, "
                    "conversation_id) VALUES (%s, %s, 1, 'running', %s)",
                    (execution_id, job_id, conversation_id))
    conn.commit()
    return execution_id


def test_a_delta_batch_resolves_its_caps_and_its_thread_once(conn, run, selects):
    """The hottest loop in the module: one fragment per streamed token. Resolving the two
    caps and the owning conversation per fragment is three reads per token."""
    sink = ConversationDeltaSink()
    try:
        sink.write([DeltaRecord(execution_id=run, seq=0, kind="delta", text="a",
                                tool_name=None)])
        selects.clear()
        sink.write([DeltaRecord(execution_id=run, seq=i, kind="delta", text="a",
                                tool_name=None) for i in range(1, 3)])
        few = len(selects)

        selects.clear()
        sink.write([DeltaRecord(execution_id=run, seq=i, kind="delta", text="a",
                                tool_name=None) for i in range(3, 9)])

        assert len(selects) == few
    finally:
        if sink._conn is not None:
            sink._conn.close()


def _timeline_rows(conn, world, cid: int, n: int) -> None:
    """`n` user turns and `n` runs: each kind the projection reads a row for."""
    for _ in range(n):
        _message_id, job_id, _ = service.submit_message(
            conn, conversation_id=cid, user_id=world["owner"].id,
            client_message_id=str(uuid.uuid4())[:16], content="p")
        with conn.cursor() as cur:
            service.append_event(cur, cid, kind="run.started", source_kind="execution",
                                 source_id=int(uuid.uuid4().int % 10**12),
                                 payload={"job_id": job_id, "attempt": 1})
        conn.commit()


def test_a_timeline_page_costs_the_same_reads_whatever_its_length(conn, world, selects):
    """The projection reads message texts and run prompts once per page, not per event."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _timeline_rows(conn, world, cid, 2)

    def page():
        rows, _ = service.list_timeline(conn, conversation_id=cid, before_id=None, limit=100)
        service.project_events(conn, rows, world["admin"],
                               service.load_visible(conn, conversation_id=cid, user=world["admin"]))

    selects.clear()
    page()
    few = len(selects)
    _timeline_rows(conn, world, cid, 6)
    selects.clear()
    page()

    assert len(selects) == few
