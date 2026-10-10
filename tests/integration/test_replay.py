"""Cursor replay over `conversation_event` (PRD E3-E5 §6.4).

The stream is a projection of a table, not the other way round: an event exists in
`conversation_event` before anything could emit it, so a client that was not listening
loses nothing. It comes back with the last id it saw and gets a bounded page.

The gate is the same `load_visible` every other read uses, so replay stops the moment the
caller loses reach - a cursor is not a grant.
"""
from __future__ import annotations

import json
import random

import pytest

from agento.modules.conversation.src import routes, service

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type, _Req  # noqa: F401


class _QReq(_Req):
    """`_Req` plus the query string the real `Request` carries."""

    def __init__(self, conn, user, params=None, query=None):
        super().__init__(conn, user, params=params)
        self.query = query or {}


@pytest.fixture
def thread(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    yield conversation_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s",
                    (conversation_id,))
    conn.commit()


def _event(conn, conversation_id, *, kind="assistant.message", payload=None,
           source_id=None, execution_id=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_event "
            "(conversation_id, execution_id, kind, payload, source_kind, source_id) "
            "VALUES (%s, %s, %s, %s, 'outbox', %s)",
            (conversation_id, execution_id, kind, json.dumps(payload or {}),
             source_id if source_id is not None else _next_source(conn)),
        )
        row_id = cur.lastrowid
    conn.commit()
    return row_id


def _next_source(conn) -> int:
    """`uq_source` is (source_kind, source_id) and the table is shared with every other
    integration test in this database, so the id has to be unique globally, not per file."""
    return random.getrandbits(48)


def _get(conn, user, conversation_id, **query):
    return routes.events(_QReq(conn, user, params={"id": str(conversation_id)}, query=query))


# --- the cursor ------------------------------------------------------------

def test_replay_from_zero_returns_every_event_oldest_first(conn, world, thread):
    first = _event(conn, thread, kind="job.claimed")
    second = _event(conn, thread, kind="assistant.message")

    response = _get(conn, world["owner"], thread, after="0")

    assert response.status == 200
    assert [e["id"] for e in response.body] == [first, second]
    assert [e["kind"] for e in response.body] == ["job.claimed", "assistant.message"]


def test_no_cursor_means_from_the_beginning(conn, world, thread):
    first = _event(conn, thread)

    assert [e["id"] for e in _get(conn, world["owner"], thread).body] == [first]


def test_a_cursor_excludes_the_event_it_names(conn, world, thread):
    first = _event(conn, thread)
    second = _event(conn, thread)

    body = _get(conn, world["owner"], thread, after=str(first)).body

    assert [e["id"] for e in body] == [second]


def test_a_cursor_past_the_end_is_an_empty_page_not_an_error(conn, world, thread):
    last = _event(conn, thread)

    response = _get(conn, world["owner"], thread, after=str(last + 1000))

    assert (response.status, response.body) == (200, [])


def test_an_empty_thread_replays_nothing(conn, world, thread):
    assert _get(conn, world["owner"], thread).body == []


def test_the_page_carries_the_id_a_client_comes_back_with(conn, world, thread):
    """The cursor is in the body: a client needs no count and no timestamp to resume."""
    _event(conn, thread)
    _event(conn, thread)

    body = _get(conn, world["owner"], thread).body
    again = _get(conn, world["owner"], thread, after=str(body[-1]["id"])).body

    assert again == []


def test_another_threads_events_are_never_in_the_page(conn, world, thread):
    other = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="other")
    mine = _event(conn, thread)
    _event(conn, other)

    assert [e["id"] for e in _get(conn, world["owner"], thread).body] == [mine]


# --- the page bound --------------------------------------------------------

def test_the_page_is_bounded_by_the_configured_page_size(conn, world, thread, monkeypatch):
    monkeypatch.setattr(service, "config", lambda conn, path: 3)
    ids = [_event(conn, thread) for _ in range(5)]

    body = _get(conn, world["owner"], thread).body

    assert [e["id"] for e in body] == ids[:3]


def test_a_page_size_above_the_ceiling_is_capped(conn, world, thread, monkeypatch):
    """An operator raising `history/page_size` must not turn one request into a whole-thread
    read: the ceiling is in the service, not in the route that happens to call it."""
    captured = {}

    def spy(sql, args):
        captured["limit"] = args[-1]
        return 0

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args): spy(sql, args)
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    service.list_events(_Conn(), conversation_id=1, after_id=0, limit=10_000)

    assert captured["limit"] == service.MAX_EVENT_PAGE == 500


def test_a_page_size_below_the_ceiling_is_untouched(conn, world, thread, monkeypatch):
    monkeypatch.setattr(service, "config", lambda conn, path: 2)
    ids = [_event(conn, thread) for _ in range(3)]

    assert len(_get(conn, world["owner"], thread).body) == 2
    assert [e["id"] for e in _get(conn, world["owner"], thread).body] == ids[:2]


# --- persist before emit ---------------------------------------------------

def test_an_event_is_in_the_table_before_anything_could_emit_it(conn, world, thread):
    """No stream is open and nothing emitted this row - it was written by the relay path and
    is readable on its own. That ordering is what makes a reconnect lossless (§6.4).

    There is deliberately nothing to assert about a stream here: the point is that replay
    does not depend on one existing. (The original version asserted the delta SINK was
    unregistered, which stopped meaning anything once Task 21 registered it - the sink is a
    writer, not a reader.)"""
    row_id = _event(conn, thread, kind="assistant.message", payload={"content": "ok"})

    body = _get(conn, world["owner"], thread).body

    assert [e["id"] for e in body] == [row_id]
    assert body[0]["payload"] == {"content": "ok"}


def test_the_payload_is_an_object_not_a_string_holding_one(conn, world, thread):
    _event(conn, thread, payload={"message_id": 7, "content": "cześć"})

    payload = _get(conn, world["owner"], thread).body[0]["payload"]

    assert payload == {"message_id": 7, "content": "cześć"}


def test_an_event_carries_its_execution_id_and_timestamp(conn, world, thread):
    _event(conn, thread, execution_id="exec-abc")

    event = _get(conn, world["owner"], thread).body[0]

    assert event["execution_id"] == "exec-abc"
    assert event["created_at"].endswith("Z")


# --- the reach gate --------------------------------------------------------

def test_replay_is_404_after_the_view_is_deactivated(conn, world, thread):
    """A cursor is not a grant: the same gate as every other read, re-checked per request."""
    _event(conn, thread)
    assert _get(conn, world["owner"], thread).status == 200

    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert _get(conn, world["owner"], thread).status == 404


def test_replay_is_404_for_a_caller_who_never_had_reach(conn, world, thread):
    _event(conn, thread)

    assert _get(conn, world["stranger"], thread).status == 404


def test_replay_of_an_unknown_thread_is_404(conn, world):
    assert _get(conn, world["owner"], 987654321).status == 404


# --- the cursor is validated ----------------------------------------------

@pytest.mark.parametrize("bad", ["-1", "abc", "1;DROP", "", "1.5", " 1"])
def test_a_cursor_that_is_not_a_number_is_400(conn, world, thread, bad):
    response = _get(conn, world["owner"], thread, after=bad)

    assert response.status == 400


def test_the_reach_gate_runs_before_the_cursor_is_parsed(conn, world, thread):
    """A 400 on an unreachable thread would confirm the thread exists."""
    assert _get(conn, world["stranger"], thread, after="nonsense").status == 404
