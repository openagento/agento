"""The SSE stream (PRD E3-E5 §7.2-§7.4).

The generator is driven directly, with `now`/`sleep` injected, rather than over a socket: the
listener's own half of the contract is asserted in tests/unit/web/test_streaming.py, and what
matters here is the loop - what it emits, when it re-checks, and when it stops.
"""
from __future__ import annotations

import json
import random

import pytest

from agento.framework.access import sessions
from agento.modules.conversation.src import service, stream

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type, _Req  # noqa: F401


@pytest.fixture
def thread(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    yield conversation_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s",
                    (conversation_id,))
    conn.commit()


def _event(conn, conversation_id, *, kind="assistant.message", payload=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_event "
            "(conversation_id, execution_id, kind, payload, source_kind, source_id) "
            "VALUES (%s, NULL, %s, %s, 'outbox', %s)",
            (conversation_id, kind, json.dumps(payload or {}), random.getrandbits(48)))
        row_id = cur.lastrowid
    conn.commit()
    return row_id


class _Clock:
    """Time the loop cannot outrun: every `sleep` advances it, so a bounded stream ends."""

    def __init__(self, step: float = 0.5) -> None:
        self.t, self.step, self.sleeps = 0.0, step, 0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += seconds or self.step


@pytest.fixture
def live_session(conn, world, monkeypatch):
    """A session that resolves to the owner until a test revokes it."""
    state = {"user": world["owner"]}
    monkeypatch.setattr(
        sessions, "lookup_session",
        lambda c, token: None if state["user"] is None
        else sessions.Session(id="sid", user=state["user"],
                              expires_at=__import__("datetime").datetime.now()
                              + __import__("datetime").timedelta(hours=1)))
    return state


def _run(conn, conversation_id, *, cursor=None, clock=None, limit=200) -> list[bytes]:
    clock = clock or _Clock()
    gen = stream.frames(conn, conversation_id=conversation_id, session_token="tok",
                        cursor=cursor, user_id=1, now=clock.now, sleep=clock.sleep)
    out = []
    for chunk in gen:
        out.append(chunk)
        if len(out) >= limit:
            gen.close()
            break
    return out


def _ids(chunks: list[bytes]) -> list[int]:
    return [int(line.split(b"id: ")[1].split(b"\n")[0])
            for line in chunks if line.startswith(b"id: ") or b"\nid: " in line]


# --- the cursor ------------------------------------------------------------

def test_no_cursor_starts_live_and_not_from_the_beginning(conn, thread, live_session):
    """A client that never asked for history must not be handed the whole thread on
    connect. Replay from zero is the `?after=0` route's job."""
    _event(conn, thread)
    _event(conn, thread)

    chunks = _run(conn, thread, cursor=None)

    assert _ids(chunks) == []


def test_a_cursor_replays_everything_after_it(conn, thread, live_session):
    first = _event(conn, thread)
    second = _event(conn, thread)
    third = _event(conn, thread)

    chunks = _run(conn, thread, cursor=first)

    assert _ids(chunks) == [second, third]


def test_a_reconnect_with_last_event_id_has_no_gap_and_no_duplicate(conn, thread,
                                                                    live_session):
    first = _event(conn, thread)
    second = _event(conn, thread)
    seen = _ids(_run(conn, thread, cursor=first))
    third = _event(conn, thread)

    again = _ids(_run(conn, thread, cursor=seen[-1]))

    assert seen == [second]
    assert again == [third]                # nothing repeated, nothing skipped


@pytest.mark.parametrize("raw", ["abc", "", "  ", "-1", "1.5", "1;2", None])
def test_a_cursor_that_is_not_a_number_means_no_cursor(raw):
    """Review Focus 5: a garbled `Last-Event-ID` is a live stream, not a 500 and not a
    replay of the whole thread."""
    assert stream.parse_cursor(raw) is None


def test_a_numeric_cursor_is_taken_as_given():
    assert stream.parse_cursor(" 42 ") == 42


def test_a_garbled_last_event_id_replays_nothing(conn, thread, live_session):
    _event(conn, thread)

    chunks = _run(conn, thread, cursor=stream.parse_cursor("not-a-number"))

    assert _ids(chunks) == []


# --- what a frame carries --------------------------------------------------

def test_each_event_goes_out_with_its_row_id(conn, thread, live_session):
    row_id = _event(conn, thread, kind="assistant.message", payload={"content": "ok"})

    chunk = _run(conn, thread, cursor=row_id - 1)[0]

    assert f"id: {row_id}\n".encode() in chunk
    assert b"event: assistant.message\n" in chunk


def test_the_frame_body_is_the_event_as_an_object(conn, thread, live_session):
    row_id = _event(conn, thread, payload={"content": "cześć"})

    chunk = _run(conn, thread, cursor=row_id - 1)[0]

    data = json.loads(chunk.split(b"data: ")[1].split(b"\n\n")[0])
    assert data["id"] == row_id and data["payload"] == {"content": "cześć"}


def test_a_delta_for_an_unseen_execution_is_rendered_anyway(conn, thread, live_session):
    """Events are self-describing (§6.4.2): a client that joined mid-run must not have to
    have seen the execution start to display what it produced."""
    row_id = _event(conn, thread, kind="assistant.delta",
                    payload={"seq": 9, "text": "fragment", "fragment": "delta"})

    chunk = _run(conn, thread, cursor=row_id - 1)[0]

    assert b"event: assistant.delta\n" in chunk
    assert json.loads(chunk.split(b"data: ")[1].split(b"\n\n")[0])["payload"]["seq"] == 9


# --- the heartbeat ---------------------------------------------------------

def test_an_idle_stream_sends_a_ping(conn, thread, live_session):
    chunks = _run(conn, thread, cursor=0, limit=1)

    assert chunks == [stream.HEARTBEAT]


def test_a_busy_stream_does_not_interleave_pings(conn, thread, live_session):
    first = _event(conn, thread)
    _event(conn, thread)

    chunks = _run(conn, thread, cursor=first - 1, limit=2)

    assert stream.HEARTBEAT not in chunks


# --- the gate, re-run every tick (§7.4, §9) --------------------------------

def test_a_revoked_session_ends_the_stream_on_the_next_tick(conn, thread, live_session):
    _event(conn, thread)
    live_session["user"] = None

    assert _run(conn, thread, cursor=0) == []


def test_a_deactivated_user_ends_the_stream(conn, thread, world):
    """Through the REAL `lookup_session`, not the stub: `u.is_active = 1` is in its own SQL,
    so a stubbed session would assert the stub and not the rule."""
    from agento.framework.access import sessions as real

    _event(conn, thread)
    _, token = real.create_session(conn, world["owner"])
    clock = _Clock()

    def run():
        gen = stream.frames(conn, conversation_id=thread, session_token=token, cursor=0,
                            user_id=world["owner"].id, now=clock.now, sleep=clock.sleep)
        out = []
        for chunk in gen:
            out.append(chunk)
            if len(out) >= 2:
                gen.close()
                break
        return out

    assert run()                                    # it streams while the user is active

    with conn.cursor() as cur:
        cur.execute("UPDATE user SET is_active = 0 WHERE id = %s", (world["owner"].id,))
    conn.commit()
    try:
        assert run() == []
    finally:
        with conn.cursor() as cur:
            cur.execute("UPDATE user SET is_active = 1 WHERE id = %s", (world["owner"].id,))
        conn.commit()


def test_a_deactivated_agent_view_ends_the_stream(conn, thread, world, live_session):
    """The tick re-runs `load_visible`, so closing the stream and answering 404 are one
    decision and cannot drift apart (§9)."""
    _event(conn, thread)
    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert _run(conn, thread, cursor=0) == []


def test_a_caller_who_never_had_reach_gets_nothing(conn, thread, world, live_session):
    _event(conn, thread)
    live_session["user"] = world["stranger"]

    assert _run(conn, thread, cursor=0) == []


def test_the_gate_is_re_run_and_not_only_checked_at_open(conn, thread, world, live_session):
    """The stream sends, then loses reach, then sends nothing more."""
    first = _event(conn, thread)
    clock = _Clock()
    gen = stream.frames(conn, conversation_id=thread, session_token="tok",
                        cursor=first - 1, user_id=1, now=clock.now, sleep=clock.sleep)
    assert next(gen).startswith(f"event: assistant.message\nid: {first}".encode())

    live_session["user"] = None

    assert list(gen) == []


# --- the bound -------------------------------------------------------------

def test_the_stream_closes_at_max_duration(conn, thread, live_session, monkeypatch):
    values = {"stream/poll_interval_ms": 500, "stream/heartbeat_seconds": 0,
              "stream/max_duration_seconds": 2, "history/page_size": 100,
              "stream/max_per_user": 4}
    monkeypatch.setattr(service, "config", lambda conn, path: values[path])
    clock = _Clock()

    chunks = _run(conn, thread, cursor=0, clock=clock, limit=1000)

    assert len(chunks) == 4                # 2 s / 0.5 s ticks, each an idle ping
    assert clock.now() >= 2


def test_the_route_is_404_for_an_unreachable_thread_before_a_frame_is_written(conn, world,
                                                                             thread):
    class _Req2:
        def __init__(self, conn, user):
            self.conn, self.params, self.query, self.headers = conn, {"id": str(thread)}, {}, {}
            self.session = type("S", (), {"user": user})()
            self.session_token = "tok"

    response = stream.open_stream(_Req2(conn, world["stranger"]))

    assert response.status == 404


def test_the_route_returns_a_stream_for_a_reachable_thread(conn, world, thread):
    from agento.web.streaming import StreamingResponse

    class _Req2:
        def __init__(self, conn, user):
            self.conn, self.params, self.query, self.headers = conn, {"id": str(thread)}, {}, {}
            self.session = type("S", (), {"user": user})()
            self.session_token = "tok"

    response = stream.open_stream(_Req2(conn, world["owner"]))

    assert isinstance(response, StreamingResponse)
    assert ("Content-Type", "text/event-stream") in response.headers
    response.frames.close()


def test_the_last_event_id_header_wins_over_the_query_parameter(conn, world, thread,
                                                               live_session):
    """The browser resends the header by itself; a stale `?after=` in the reconnect URL must
    not drag the client back to where it started."""
    first = _event(conn, thread)
    second = _event(conn, thread)

    class _Req2:
        def __init__(self):
            self.conn, self.params = conn, {"id": str(thread)}
            self.query, self.headers = {"after": "0"}, {"Last-Event-ID": str(first)}
            self.session = type("S", (), {"user": world["owner"]})()
            self.session_token = "tok"

    gen = stream.open_stream(_Req2()).frames
    try:
        assert f"id: {second}\n".encode() in next(gen)
    finally:
        gen.close()


@pytest.mark.parametrize("raw", ["not-a-number", "1.5", "-1", "1;2", "", "   "])
def test_a_garbled_last_event_id_beats_after_zero(conn, world, thread, live_session, raw):
    """A resume the browser mangled must not fall through to `?after=0` and replay the whole
    thread. The header WINS whenever it was sent, garbled or not; `?after` answers only a
    client that sent no header at all.

    A BLANK header is sent, so it wins too: presence is the question, not the value. Reading
    blank as absent handed `?after=0` a client that only meant to resume."""
    first = _event(conn, thread)
    _event(conn, thread)

    class _Req2:
        def __init__(self):
            self.conn, self.params = conn, {"id": str(thread)}
            self.query, self.headers = {"after": "0"}, {"Last-Event-ID": raw}
            self.session = type("S", (), {"user": world["owner"]})()
            self.session_token = "tok"

    gen = stream.open_stream(_Req2()).frames
    try:
        chunk = next(gen)
    finally:
        gen.close()
    assert f"id: {first}\n".encode() not in chunk


def test_no_header_at_all_still_honours_after(conn, world, thread, live_session):
    """The fall-back survives: `?after=0` is the documented replay route for a client that
    sent no `Last-Event-ID`."""
    first = _event(conn, thread)

    class _Req2:
        def __init__(self):
            self.conn, self.params = conn, {"id": str(thread)}
            self.query, self.headers = {"after": "0"}, {}
            self.session = type("S", (), {"user": world["owner"]})()
            self.session_token = "tok"

    gen = stream.open_stream(_Req2()).frames
    try:
        assert f"id: {first}\n".encode() in next(gen)
    finally:
        gen.close()


def test_a_fully_pruned_thread_opened_with_no_cursor_is_live(conn, world, thread, live_session):
    """No cursor can never expire (docs/architecture/conversations.md). Substituting the
    newest event id BEFORE asking broke that where it matters most: a thread whose events
    are all pruned has no newest id, so the substitute was 0, and 0 is at or below every
    watermark - the one case where a reader has nothing to be told about."""
    first = _event(conn, thread)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s", (thread,))
        cur.execute("INSERT INTO conversation_prune_watermark "
                    "(conversation_id, last_pruned_event_id) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE last_pruned_event_id = VALUES(last_pruned_event_id)",
                    (thread, first))
    conn.commit()

    class _Req2:
        def __init__(self):
            self.conn, self.params = conn, {"id": str(thread)}
            self.query, self.headers = {}, {}
            self.session = type("S", (), {"user": world["owner"]})()
            self.session_token = "tok"

    gen = stream.open_stream(_Req2()).frames
    try:
        chunk = next(gen)
    finally:
        gen.close()
    assert b"cursor_expired" not in chunk

    # And a cursor the caller DID supply below that watermark still expires.
    class _Req3(_Req2):
        def __init__(self):
            super().__init__()
            self.headers = {"Last-Event-ID": str(first)}

    gen = stream.open_stream(_Req3()).frames
    try:
        assert b"cursor_expired" in next(gen)
    finally:
        gen.close()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_prune_watermark WHERE conversation_id = %s",
                    (thread,))
    conn.commit()
