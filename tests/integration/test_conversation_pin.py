"""Pin, rename and the list they reorder (E9 chat UX).

A pin changes two things that are easy to get wrong: an aggregate cap under concurrency,
and an ordering that must not hide the new threads it sorts below.
"""
from __future__ import annotations

import threading

import pytest

from agento.modules.conversation.src import retention, routes, service

from .conftest import _test_connection


class _Session:
    def __init__(self, user):
        self.user = user


class _Req:
    def __init__(self, conn, user, params=None, json=None):
        self.conn, self.session = conn, _Session(user)
        self.params, self.json, self.query = params or {}, json, {}


def _conversation(conn, world, title="t") -> int:
    return service.create_conversation(conn, user_id=world["owner"].id,
                                       agent_view_id=world["view"], title=title)


def _pinned_at(conn, cid):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT pinned_at FROM conversation WHERE id = %s", (cid,))
        row = cur.fetchone()
    conn.commit()
    return row["pinned_at"]


# --- the route --------------------------------------------------------------

def test_the_route_renames_pins_and_unpins(conn, world):
    cid = _conversation(conn, world)

    renamed = routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                                 json={"title": "Faktury"}))
    assert renamed.status == 200 and renamed.body["title"] == "Faktury"

    pinned = routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                                json={"pinned": True}))
    assert pinned.status == 200 and pinned.body["pinned"] is True
    assert _pinned_at(conn, cid) is not None

    unpinned = routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                                  json={"pinned": False}))
    assert unpinned.body["pinned"] is False and _pinned_at(conn, cid) is None


def test_the_route_refuses_a_thread_that_is_not_the_callers(conn, world):
    cid = _conversation(conn, world)

    assert routes.update(_Req(conn, world["stranger"], params={"id": str(cid)},
                              json={"title": "x"})).status == 404


def test_the_route_refuses_an_empty_body_and_a_bad_title(conn, world):
    cid = _conversation(conn, world)

    assert routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                              json={})).status == 400
    assert routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                              json={"title": "x" * (service.MAX_TITLE + 1)})).status == 400
    assert routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                              json={"pinned": "yes"})).status == 400


def _pin_many(conn, world, n: int) -> list[int]:
    ids = [_conversation(conn, world, title=f"t{i}") for i in range(n)]
    conn.commit()
    with conn.cursor() as cur:
        cur.executemany("UPDATE conversation SET pinned_at = NOW() WHERE id = %s",
                        [(i,) for i in ids])
    conn.commit()
    return ids


def test_a_refused_field_leaves_the_other_one_unchanged(conn, world):
    """One transaction, after every field is validated: a 400 or a 409 writes nothing."""
    cid = _conversation(conn, world, title="before")
    _pin_many(conn, world, service.MAX_PINNED)

    assert routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                              json={"title": "after", "pinned": "yes"})).status == 400
    assert routes.update(_Req(conn, world["owner"], params={"id": str(cid)},
                              json={"title": "after", "pinned": True})).status == 409

    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT title, pinned_at FROM conversation WHERE id = %s", (cid,))
        row = cur.fetchone()
    conn.commit()
    assert row["title"] == "before" and row["pinned_at"] is None


def test_two_racing_route_calls_at_the_cap_minus_one_let_exactly_one_through(conn, world):
    """The route reads the thread first. That read opens the transaction's REPEATABLE READ
    snapshot, so the cap's count must be a LOCKING read or both callers still count 49."""
    _pin_many(conn, world, service.MAX_PINNED - 1)
    racers = [_conversation(conn, world, title="a"), _conversation(conn, world, title="b")]
    conn.commit()

    codes: list = []
    start = threading.Barrier(2)
    read = threading.Barrier(2)

    def patch(cid):
        own = _test_connection(autocommit=False)
        try:
            start.wait(timeout=10)
            req = _Req(own, world["owner"], params={"id": str(cid)}, json={"pinned": True})
            service.load_visible(own, conversation_id=cid, user=world["owner"])  # the gate's read
            read.wait(timeout=10)                 # both snapshots are open before either pins
            codes.append(routes.update(req).status)
        finally:
            own.close()

    threads = [threading.Thread(target=patch, args=(c,)) for c in racers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sorted(codes) == [200, 409]
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM conversation WHERE user_id = %s "
                    "AND pinned_at IS NOT NULL", (world["owner"].id,))
        assert cur.fetchone()["n"] == service.MAX_PINNED
    conn.commit()


# --- the cap ----------------------------------------------------------------

def test_one_pin_past_the_cap_is_refused(conn, world):
    _pin_many(conn, world, service.MAX_PINNED)
    extra = _conversation(conn, world, title="one too many")

    with pytest.raises(service.SubmissionError) as caught:
        service.set_pinned(conn, extra, user_id=world["owner"].id, pinned=True)

    assert caught.value.status == 409
    assert _pinned_at(conn, extra) is None


def test_two_racing_pins_at_the_cap_minus_one_let_exactly_one_through(conn, world):
    """The cap is an aggregate: two pins of DIFFERENT threads take different row locks, so
    only the per-user lock can serialize them."""
    _pin_many(conn, world, service.MAX_PINNED - 1)
    racers = [_conversation(conn, world, title="a"), _conversation(conn, world, title="b")]
    conn.commit()

    results: list = []
    start = threading.Barrier(2)

    def pin(cid):
        own = _test_connection(autocommit=False)
        try:
            start.wait(timeout=10)
            service.set_pinned(own, cid, user_id=world["owner"].id, pinned=True)
            results.append(True)
        except service.SubmissionError:
            results.append(False)
        finally:
            own.close()

    threads = [threading.Thread(target=pin, args=(c,)) for c in racers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sorted(results) == [False, True]
    with conn.cursor() as cur:
        conn.commit()
        cur.execute("SELECT COUNT(*) AS n FROM conversation WHERE user_id = %s "
                    "AND pinned_at IS NOT NULL", (world["owner"].id,))
        assert cur.fetchone()["n"] == service.MAX_PINNED
    conn.commit()


# --- the ordering -----------------------------------------------------------

def test_a_full_set_of_pins_still_leaves_a_whole_page_of_new_threads(conn, world):
    """Pinned-first with one LIMIT is how a rail stops showing new conversations."""
    _pin_many(conn, world, service.MAX_PINNED)
    fresh = [_conversation(conn, world, title=f"new{i}") for i in range(5)]
    conn.commit()

    rows = service.list_visible(conn, user=world["owner"], limit=5)

    assert [r["id"] for r in rows[:service.MAX_PINNED]] != []
    assert set(fresh) <= {r["id"] for r in rows}
    assert all(r["pinned_at"] is not None for r in rows[:service.MAX_PINNED])


def test_the_idle_pass_leaves_a_pinned_thread_alone(conn, world):
    kept = _conversation(conn, world, title="kept")
    dropped = _conversation(conn, world, title="dropped")
    service.set_pinned(conn, kept, user_id=world["owner"].id, pinned=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET last_activity_at = NOW() - INTERVAL 100 DAY, "
                    "created_at = NOW() - INTERVAL 100 DAY WHERE id IN (%s, %s)",
                    (kept, dropped))
    conn.commit()

    assert retention.auto_archive(conn, idle_days=30) == 1

    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM conversation WHERE id IN (%s, %s)", (kept, dropped))
        status = {r["id"]: r["status"] for r in cur.fetchall()}
    conn.commit()
    assert status == {kept: "active", dropped: "archived"}
