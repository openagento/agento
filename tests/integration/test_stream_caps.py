"""The per-user stream cap (PRD E3-E5 §7.3).

The budget is threads: `web` is a `ThreadingHTTPServer` and one open streaming response costs
exactly one thread (measured 1:1, released on close). Exceeding the cap closes the **oldest**
stream for that user and never refuses the new one - a refusal would make a reconnect storm
self-inflicted denial of service, because the client whose stream just dropped is precisely
the one asking again.
"""
from __future__ import annotations

import json

import pytest

from agento.framework.access import sessions
from agento.modules.conversation.src import service, stream

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_registry():
    stream._open_streams.clear()
    yield
    stream._open_streams.clear()


@pytest.fixture
def thread(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    yield conversation_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s",
                    (conversation_id,))
    conn.commit()


@pytest.fixture
def cap(monkeypatch):
    values = {"stream/poll_interval_ms": 500, "stream/heartbeat_seconds": 0,
              "stream/max_duration_seconds": 3600, "history/page_size": 100,
              "stream/max_per_user": 3}
    monkeypatch.setattr(service, "config", lambda conn, path: values[path])
    return values


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds or 0.5


@pytest.fixture
def signed_in(monkeypatch, world):
    import datetime

    monkeypatch.setattr(
        sessions, "lookup_session",
        lambda c, token: sessions.Session(
            id="sid", user=world["owner"],
            expires_at=datetime.datetime.now() + datetime.timedelta(hours=1)))


def _open(conn, thread, user_id: int):
    """A started generator - a slot is taken when the loop runs, not when it is built."""
    clock = _Clock()
    gen = stream.frames(conn, conversation_id=thread, session_token="tok", cursor=0,
                        user_id=user_id, now=clock.now, sleep=clock.sleep)
    next(gen)                    # the idle ping; the loop is now inside its `try`
    return gen


def _still_open(gen) -> bool:
    try:
        next(gen)
        return True
    except StopIteration:
        return False


# --- the cap ---------------------------------------------------------------

def test_opening_one_over_the_cap_closes_the_oldest(conn, thread, world, cap, signed_in):
    uid = world["owner"].id
    streams = [_open(conn, thread, uid) for _ in range(cap["stream/max_per_user"])]

    newest = _open(conn, thread, uid)

    assert _still_open(streams[0]) is False       # the oldest is told to stop
    assert _still_open(newest) is True            # the newest is never the one refused


def test_the_newest_stream_always_survives(conn, thread, world, cap, signed_in):
    uid = world["owner"].id
    for _ in range(10):
        newest = _open(conn, thread, uid)
        assert _still_open(newest) is True


def test_a_reconnect_storm_converges_on_exactly_the_cap(conn, thread, world, cap,
                                                        signed_in):
    """Review Focus 4. A storm must not close one extra per still-draining generator, and
    must never close the stream it has just opened."""
    uid = world["owner"].id
    live = []
    for _ in range(25):
        gen = _open(conn, thread, uid)
        live.append(gen)
        assert _still_open(gen) is True           # never the one just opened

    assert stream.live_streams(uid) == cap["stream/max_per_user"]


def test_below_the_cap_nothing_is_closed(conn, thread, world, cap, signed_in):
    uid = world["owner"].id
    streams = [_open(conn, thread, uid) for _ in range(cap["stream/max_per_user"])]

    assert all(_still_open(g) for g in streams)
    assert stream.live_streams(uid) == cap["stream/max_per_user"]


def test_one_users_streams_never_close_anothers(conn, thread, world, cap, signed_in):
    mine = [_open(conn, thread, world["owner"].id)
            for _ in range(cap["stream/max_per_user"])]

    for _ in range(5):
        _open(conn, thread, world["stranger"].id)

    assert all(_still_open(g) for g in mine)
    assert stream.live_streams(world["owner"].id) == cap["stream/max_per_user"]


# --- the slot is released --------------------------------------------------

def test_closing_a_stream_releases_its_slot(conn, thread, world, cap, signed_in):
    uid = world["owner"].id
    gen = _open(conn, thread, uid)
    assert stream.live_streams(uid) == 1

    gen.close()

    assert stream.live_streams(uid) == 0


def test_a_closed_stream_leaves_no_user_entry_behind(conn, thread, world, cap, signed_in):
    uid = world["owner"].id
    _open(conn, thread, uid).close()

    assert uid not in stream._open_streams


def test_an_evicted_stream_releases_its_slot_when_its_loop_notices(conn, thread, world,
                                                                   cap, signed_in):
    uid = world["owner"].id
    streams = [_open(conn, thread, uid) for _ in range(cap["stream/max_per_user"] + 1)]

    assert _still_open(streams[0]) is False       # it ran its `finally`
    assert stream.live_streams(uid) == cap["stream/max_per_user"]


def test_a_generator_that_is_never_started_takes_no_slot(conn, thread, world, cap,
                                                         signed_in):
    """The slot is taken inside the generator body, so building one and dropping it leaks
    nothing - which is what the 404 path does."""
    stream.frames(conn, conversation_id=thread, session_token="tok", cursor=0,
                  user_id=world["owner"].id)

    assert stream.live_streams(world["owner"].id) == 0


# --- the cap value itself --------------------------------------------------

def test_a_cap_of_zero_still_leaves_the_newest_stream_open(conn, thread, world, monkeypatch,
                                                           signed_in):
    """An operator typo must not make chat unusable: something is always streaming."""
    values = {"stream/poll_interval_ms": 500, "stream/heartbeat_seconds": 0,
              "stream/max_duration_seconds": 3600, "history/page_size": 100,
              "stream/max_per_user": 0}
    monkeypatch.setattr(service, "config", lambda conn, path: values[path])

    gen = _open(conn, thread, world["owner"].id)

    assert _still_open(gen) is True
    assert stream.live_streams(world["owner"].id) == 1


def test_the_shipped_cap_is_what_the_module_declares():
    from pathlib import Path

    config = json.loads(
        (Path(service.MODULE_DIR) / "config.json").read_text())

    assert config["stream/max_per_user"] >= 1
