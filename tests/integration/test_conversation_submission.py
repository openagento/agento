"""Conversation submission, the reach gate and the sweep (PRD E3-E5 §4.1, §4.4, §9)."""
from __future__ import annotations

import threading

import pytest

from agento.framework.access import accounts
from agento.framework.job_types import clear_job_types, register_job_type
from agento.modules.conversation.src import routes, service

from .conftest import _test_connection


@pytest.fixture(autouse=True)
def _job_type():
    register_job_type("conversation", module="conversation")
    yield
    clear_job_types()


def _conversation(conn, world, user=None, view="view") -> int:
    return service.create_conversation(
        conn, user_id=(user or world["owner"]).id,
        agent_view_id=world[view] if view else None, title="t")


def _submit(conn, conversation_id, world, cmid="c1", content="hello"):
    return service.submit_message(conn, conversation_id=conversation_id,
                                  user_id=world["owner"].id,
                                  client_message_id=cmid, content=content)


def _rows(conn, sql, params=()) -> list[dict]:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


# --- the three-step contract ----------------------------------------------

def test_a_submission_publishes_one_job_and_announces_one_turn(conn, world):
    cid = _conversation(conn, world)

    message_id, job_id, created = _submit(conn, cid, world)

    assert created is True
    jobs = _rows(conn, "SELECT * FROM job")
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["requester_trust"] == "account"
    assert jobs[0]["reference_id"] == f"{cid}:{message_id}"
    assert jobs[0]["prompt"] == "hello"
    assert _rows(conn, "SELECT job_state FROM message")[0]["job_state"] == "published"
    events = _rows(conn, "SELECT * FROM conversation_event")
    assert [e["kind"] for e in events] == ["message.created"]


def test_the_same_client_message_id_is_one_message_and_one_job(conn, world):
    cid = _conversation(conn, world)

    first = _submit(conn, cid, world)
    second = _submit(conn, cid, world)

    assert second == (first[0], first[1], False)
    assert len(_rows(conn, "SELECT id FROM message")) == 1
    assert len(_rows(conn, "SELECT id FROM job")) == 1
    assert len(_rows(conn, "SELECT id FROM conversation_event")) == 1  # one turn, one announcement


def test_two_concurrent_submissions_resolve_to_one_turn(conn, world):
    cid = _conversation(conn, world)
    start = threading.Barrier(2)
    results: list = []

    def submit():
        c = _test_connection(autocommit=False)
        try:
            start.wait(timeout=5)
            results.append(service.submit_message(
                c, conversation_id=cid, user_id=world["owner"].id,
                client_message_id="race", content="hello"))
        finally:
            c.close()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(results) == 2
    assert results[0][0] == results[1][0]        # one message
    assert results[0][1] == results[1][1]        # one job
    assert sorted(r[2] for r in results) == [False, True]
    assert len(_rows(conn, "SELECT id FROM job")) == 1
    assert len(_rows(conn, "SELECT id FROM conversation_event")) == 1


def test_a_crash_before_the_publish_leaves_a_pending_message_the_sweep_finishes(conn, world, monkeypatch):
    cid = _conversation(conn, world)

    def boom(**kwargs):
        raise RuntimeError("the publisher died")

    publishing = service.publish_job
    monkeypatch.setattr(service, "publish_job", boom)
    with pytest.raises(RuntimeError):
        _submit(conn, cid, world)
    # Restored by hand: monkeypatch.undo() would also drop the fixture's test-database
    # wiring and send the sweep at the production host.
    monkeypatch.setattr(service, "publish_job", publishing)

    rows = _rows(conn, "SELECT id, job_state FROM message")
    assert [r["job_state"] for r in rows] == ["pending"]

    assert service.sweep_pending(conn, grace_seconds=0) == 1
    assert _rows(conn, "SELECT job_state FROM message")[0]["job_state"] == "published"
    assert len(_rows(conn, "SELECT id FROM job")) == 1
    assert service.sweep_pending(conn, grace_seconds=0) == 0    # a second tick is a no-op


def test_completing_twice_publishes_once(conn, world):
    cid = _conversation(conn, world)
    message_id, job_id, _ = _submit(conn, cid, world)

    assert service.complete_pending(conn, message_id) == job_id
    assert len(_rows(conn, "SELECT id FROM job")) == 1


# --- input validation ------------------------------------------------------

def test_a_message_over_the_byte_limit_is_refused_before_any_insert(conn, world):
    _conversation(conn, world)
    limit = service.config(conn, "limits/max_message_bytes")

    with pytest.raises(service.SubmissionError) as exc:
        service.check_content(conn, "z" * (limit + 1))

    assert exc.value.status == 413
    assert _rows(conn, "SELECT id FROM message") == []


def test_the_limit_counts_utf8_bytes_not_characters(conn, world):
    limit = service.config(conn, "limits/max_message_bytes")

    # Two bytes each: half the limit in characters is exactly the limit in bytes.
    service.check_content(conn, "ą" * (limit // 2))
    with pytest.raises(service.SubmissionError):
        service.check_content(conn, "ą" * (limit // 2 + 1))


@pytest.mark.parametrize("value", ["", "x" * 129, None, 7, b"\xff"])
def test_a_bad_client_message_id_is_refused(conn, value):
    with pytest.raises(service.SubmissionError) as exc:
        service.check_client_message_id(value)

    assert exc.value.status == 400


# --- the one reach gate (§9) ----------------------------------------------

def test_the_owner_reads_their_own_conversation(conn, world):
    cid = _conversation(conn, world)

    assert service.load_visible(conn, conversation_id=cid, user=world["owner"])["id"] == cid
    assert [c["id"] for c in service.list_visible(conn, user=world["owner"], limit=10)] == [cid]


def test_another_users_conversation_is_invisible(conn, world):
    cid = _conversation(conn, world)

    assert service.load_visible(conn, conversation_id=cid, user=world["stranger"]) is None
    assert service.list_visible(conn, user=world["stranger"], limit=10) == []


def test_a_deactivated_view_hides_the_conversation_from_the_next_request(conn, world):
    cid = _conversation(conn, world)
    assert service.load_visible(conn, conversation_id=cid, user=world["owner"]) is not None

    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert service.load_visible(conn, conversation_id=cid, user=world["owner"]) is None
    assert service.list_visible(conn, user=world["owner"], limit=10) == []


def test_a_deactivated_workspace_hides_it_too(conn, world):
    cid = _conversation(conn, world)

    with conn.cursor() as cur:
        cur.execute("UPDATE workspace SET is_active = 0 WHERE id = %s", (world["workspace"],))
    conn.commit()

    assert service.load_visible(conn, conversation_id=cid, user=world["owner"]) is None


def test_the_active_gate_binds_admin_too(conn, world):
    cid = _conversation(conn, world)

    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert service.load_visible(conn, conversation_id=cid, user=world["admin"]) is None


def test_removing_the_grant_hides_the_conversation(conn, world):
    cid = _conversation(conn, world)
    grant = accounts.list_grants(conn, "user")[0]

    accounts.remove_grant(conn, grant["id"])

    assert service.load_visible(conn, conversation_id=cid, user=world["owner"]) is None


def test_a_deleted_view_leaves_the_thread_readable_to_its_owner(conn, world):
    """`ON DELETE SET NULL`: no scope left to reach, and none left to deactivate."""
    cid = _conversation(conn, world)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_view WHERE id = %s", (world["view"],))
    conn.commit()

    row = service.load_visible(conn, conversation_id=cid, user=world["owner"])
    assert row is not None and row["agent_view_id"] is None
    assert service.load_visible(conn, conversation_id=cid, user=world["stranger"]) is None


def test_can_reach_is_unchanged_by_the_new_predicate(conn, world):
    """E2's admin screens administer a deactivated view; `can_reach` must still say yes."""
    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert accounts.can_reach(conn, world["owner"], workspace_id=world["workspace"],
                              agent_view_id=world["view"]) is True
    assert accounts.scope_is_active(conn, world["view"]) is False
    assert accounts.scope_is_active(conn, None) is True


# --- terminal reconciliation (PRD:172) ------------------------------------

def _set_job_status(conn, job_id, status):
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = %s WHERE id = %s", (status, job_id))
    conn.commit()


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED", "DEAD"])
def test_a_finished_job_advances_its_message_to_terminal(conn, world, status):
    cid = _conversation(conn, world)
    _, job_id, _ = _submit(conn, cid, world)
    _set_job_status(conn, job_id, status)

    assert service.reconcile_terminal(conn) == 1
    assert _rows(conn, "SELECT job_state FROM message")[0]["job_state"] == "terminal"
    assert service.reconcile_terminal(conn) == 0


@pytest.mark.parametrize("status", ["TODO", "RUNNING", "PAUSED"])
def test_an_unfinished_job_is_left_alone(conn, world, status):
    cid = _conversation(conn, world)
    _submit(conn, cid, world)
    _set_job_status(conn, _rows(conn, "SELECT id FROM job")[0]["id"], status)

    assert service.reconcile_terminal(conn) == 0
    assert _rows(conn, "SELECT job_state FROM message")[0]["job_state"] == "published"


def test_the_next_turn_is_not_blocked_for_ever_by_a_missing_finalizer(conn, world):
    """The end-to-end case the reconciliation exists for (§4.4 reads `job_state`)."""
    cid = _conversation(conn, world)
    _, job_id, _ = _submit(conn, cid, world)
    _set_job_status(conn, job_id, "SUCCESS")     # no finalizer registered in E3

    service.reconcile_terminal(conn)

    states = [r["job_state"] for r in _rows(conn, "SELECT job_state FROM message ORDER BY id")]
    assert states == ["terminal"]
    second = _submit(conn, cid, world, cmid="c2", content="again")
    assert second[2] is True and second[1] != job_id


# --- the routes ------------------------------------------------------------

class _Req:
    def __init__(self, conn, user, params=None, json=None, query=None):
        self.conn, self.session, self.params, self.json = conn, _Session(user), params or {}, json
        self.query = query or {}


class _Session:
    def __init__(self, user):
        self.user = user


def test_a_conversation_against_an_unreachable_view_is_404(conn, world):
    """A grant is per ROLE (E2), so "unreachable" is a scope no grant covers - c-av2."""
    response = routes.create(_Req(conn, world["owner"],
                                  json={"agent_view_id": world["other_view"]}))

    assert response.status == 404
    assert _rows(conn, "SELECT id FROM conversation") == []


def test_an_unknown_view_is_404(conn, world):
    assert routes.create(_Req(conn, world["owner"], json={"agent_view_id": 987654})).status == 404


def test_create_then_read_through_the_routes(conn, world):
    created = routes.create(_Req(conn, world["owner"], json={"agent_view_id": world["view"],
                                                             "title": "hi"}))
    assert created.status == 201
    cid = str(created.body["id"])

    assert routes.show(_Req(conn, world["owner"], params={"id": cid})).status == 200
    assert routes.index(_Req(conn, world["owner"])).status == 200
    assert routes.messages(_Req(conn, world["owner"], params={"id": cid})).status == 200


def test_every_read_route_is_404_after_the_view_is_deactivated(conn, world):
    cid = str(_conversation(conn, world))
    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert routes.show(_Req(conn, world["owner"], params={"id": cid})).status == 404
    assert routes.messages(_Req(conn, world["owner"], params={"id": cid})).status == 404
    assert routes.index(_Req(conn, world["owner"])).body == []


def test_every_read_route_is_404_after_the_grant_is_removed(conn, world):
    cid = str(_conversation(conn, world))
    accounts.remove_grant(conn, accounts.list_grants(conn, "user")[0]["id"])

    assert routes.show(_Req(conn, world["owner"], params={"id": cid})).status == 404
    assert routes.messages(_Req(conn, world["owner"], params={"id": cid})).status == 404
    assert routes.index(_Req(conn, world["owner"])).body == []


def test_a_deleted_view_leaves_the_read_routes_answering_200(conn, world):
    cid = str(_conversation(conn, world))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_view WHERE id = %s", (world["view"],))
    conn.commit()

    assert routes.show(_Req(conn, world["owner"], params={"id": cid})).status == 200
    assert routes.messages(_Req(conn, world["owner"], params={"id": cid})).status == 200
    assert len(routes.index(_Req(conn, world["owner"])).body) == 1
    # ... and nothing left to run the turn on.
    assert routes.send(_Req(conn, world["owner"], params={"id": cid},
                            json={"client_message_id": "x", "content": "hi"})).status == 404


def test_another_users_conversation_is_404_never_403(conn, world):
    cid = str(_conversation(conn, world))

    assert routes.show(_Req(conn, world["stranger"], params={"id": cid})).status == 404
    assert routes.send(_Req(conn, world["stranger"], params={"id": cid},
                            json={"client_message_id": "x", "content": "hi"})).status == 404


def test_send_answers_201_then_200(conn, world):
    cid = str(_conversation(conn, world))
    body = {"client_message_id": "c1", "content": "hello"}

    first = routes.send(_Req(conn, world["owner"], params={"id": cid}, json=body))
    second = routes.send(_Req(conn, world["owner"], params={"id": cid}, json=body))

    assert (first.status, second.status) == (201, 200)
    assert first.body == second.body


def test_send_refuses_a_bad_client_message_id_before_any_insert(conn, world):
    cid = str(_conversation(conn, world))

    response = routes.send(_Req(conn, world["owner"], params={"id": cid},
                                json={"client_message_id": "", "content": "hi"}))

    assert response.status == 400
    assert _rows(conn, "SELECT id FROM message") == []


def test_send_refuses_an_oversized_message_before_any_insert(conn, world):
    cid = str(_conversation(conn, world))
    limit = service.config(conn, "limits/max_message_bytes")

    response = routes.send(_Req(conn, world["owner"], params={"id": cid},
                                json={"client_message_id": "c1", "content": "z" * (limit + 1)}))

    assert response.status == 413
    assert _rows(conn, "SELECT id FROM message") == []


def test_archiving_hides_the_thread_from_the_list_but_keeps_it_readable(conn, world):
    """Archiving is not deletion and not a close: the thread leaves the list and stays
    readable, and a post into it revives it (§3.2), which the reactivation tests below own."""
    cid = str(_conversation(conn, world))

    assert routes.destroy(_Req(conn, world["owner"], params={"id": cid})).status == 200
    assert routes.index(_Req(conn, world["owner"])).body == []
    assert routes.show(_Req(conn, world["owner"], params={"id": cid})).status == 200


# --- a post into an archived thread reactivates it (§3.2) -------------------

def _archive(conn, conversation_id) -> None:
    service.archive(conn, int(conversation_id), actor_id=None, reason="manual")


def test_a_post_into_an_archived_thread_reactivates_it(conn, world):
    cid = str(_conversation(conn, world))
    _archive(conn, cid)

    response = routes.send(_Req(conn, world["owner"], params={"id": cid},
                                json={"client_message_id": "c1", "content": "hi"}))

    assert response.status == 201
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM conversation WHERE id = %s", (cid,))
        assert cur.fetchone()["status"] == "active"
    conn.commit()


def test_reactivation_dispatches_once_and_not_for_a_live_thread(conn, world):
    """The event is a real lifecycle change, so a post into a thread that was never
    archived must not announce one."""
    from agento.framework.event_manager import ObserverEntry, clear, get_event_manager

    clear()
    seen: list = []
    get_event_manager().register("conversation_reactivate_after", ObserverEntry(
        name="spy", observer_class=type("Spy", (), {
            "execute": lambda self, event: seen.append(event)})))
    try:
        cid = str(_conversation(conn, world))
        routes.send(_Req(conn, world["owner"], params={"id": cid},
                         json={"client_message_id": "c1", "content": "hi"}))
        assert seen == []

        _archive(conn, cid)
        routes.send(_Req(conn, world["owner"], params={"id": cid},
                         json={"client_message_id": "c2", "content": "again"}))

        assert [e.conversation_id for e in seen] == [int(cid)]
    finally:
        clear()


def test_a_refused_body_does_not_revive_an_archived_thread(conn, world):
    """Validation first: a post that never lands must not leave the thread live."""
    cid = str(_conversation(conn, world))
    _archive(conn, cid)

    assert routes.send(_Req(conn, world["owner"], params={"id": cid},
                            json={"client_message_id": "", "content": "hi"})).status == 400
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM conversation WHERE id = %s", (cid,))
        assert cur.fetchone()["status"] == "archived"
    conn.commit()


def test_the_revival_and_the_new_turn_commit_together(conn, world, monkeypatch):
    """A retention pass that runs mid-post finds the thread archived, never revived-and-empty.

    The gap this closes: with the revival in its own earlier transaction, the thread is
    `active` with no new message for as long as the insert takes, and §10.1's pass is
    entitled to archive exactly that - so the post succeeds into a thread that is archived
    again, and nothing reactivates it a second time.
    """
    from agento.modules.conversation.src import retention

    cid = _conversation(conn, world)
    _archive(conn, cid)
    with conn.cursor() as cur:               # old enough for the idle pass to want it
        cur.execute("UPDATE conversation SET created_at = NOW() - INTERVAL 100 DAY "
                    "WHERE id = %s", (cid,))
    conn.commit()

    ran: list = []
    real_event = service.append_event

    def barrier(cur, conversation_id, **kwargs):
        """Called inside the post's transaction, right after the message insert."""
        real_event(cur, conversation_id, **kwargs)
        outside = _test_connection(autocommit=False)
        try:
            ran.append(retention.auto_archive(outside, idle_days=30))
        finally:
            outside.close()

    monkeypatch.setattr(service, "append_event", barrier)
    service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                           client_message_id="c1", content="hi", reactivate_actor_id=1)

    assert ran == [0]                        # the pass saw an archived thread, not a live empty one
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM conversation WHERE id = %s", (cid,))
        assert cur.fetchone()["status"] == "active"
        cur.execute("SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (cid,))
        assert cur.fetchone()["n"] == 1
    conn.commit()


def test_an_event_announces_a_transition_that_happened_and_nothing_else(conn, world):
    """A second archive, and a delete of a thread that is not there, change nothing - so
    they announce nothing. An observer that fires on a no-op reacts to a state change that
    never occurred."""
    from agento.framework.event_manager import ObserverEntry, clear, get_event_manager

    clear()
    seen: list = []
    for name in ("conversation_archive_after", "conversation_delete_after"):
        get_event_manager().register(name, ObserverEntry(
            name=f"spy-{name}", observer_class=type("Spy", (), {
                "execute": lambda self, event: seen.append(event)})))
    try:
        cid = _conversation(conn, world)

        assert service.archive(conn, cid) is True
        assert service.archive(conn, cid) is False           # already archived
        assert service.delete_conversation(conn, cid) is True
        assert service.delete_conversation(conn, cid) is False  # already gone

        assert len(seen) == 2
    finally:
        clear()


def test_a_lone_surrogate_is_refused_at_every_text_input(conn, world):
    """JSON can carry `"\\ud800"`; UTF-8 cannot encode it. The gate answers 400 - past it the
    first encode raises and the caller gets a 500 for an input that was theirs to fix.

    The class: every text input this module takes from a request body. `json.loads` is the
    real path - it is what produces the lone surrogate no literal in this file could.
    """
    import json as _json
    bad = _json.loads('"\\ud800"')

    created = routes.create(_Req(conn, world["owner"], json={"agent_view_id": world["view"]}))
    cid = str(created.body["id"])

    assert routes.create(_Req(conn, world["owner"],
                              json={"agent_view_id": world["view"],
                                    "title": bad})).status == 400
    assert routes.send(_Req(conn, world["owner"], params={"id": cid},
                            json={"client_message_id": bad, "content": "ok"})).status == 400
    assert routes.send(_Req(conn, world["owner"], params={"id": cid},
                            json={"client_message_id": "c1", "content": bad})).status == 400
