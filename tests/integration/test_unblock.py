"""Unblocking a stuck turn (PRD E3-E5 §4.5).

§5.3 names the one state a thread cannot leave on its own: a turn whose job is `PAUSED`
with `session_id IS NULL`, which `resume_job()` refuses for ever and which §4.4 then makes
block every later turn. This is the way out, and it is guarded twice.
"""
from __future__ import annotations

import uuid

import pytest

from agento.framework.event_manager import ObserverEntry, clear, get_event_manager
from agento.modules.conversation.src import routes, service

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type, _Req  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_executions(conn):
    def wipe():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM execution")
        conn.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def stuck(conn, world):
    """A turn whose job is PAUSED with no session - §5.3's trap."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'PAUSED', session_id = NULL WHERE id = %s",
                    (job_id,))
    conn.commit()
    return {"conversation_id": conversation_id, "message_id": message_id, "job_id": job_id}


@pytest.fixture
def seen():
    clear()
    captured: list = []
    get_event_manager().register("conversation_unblock_after", ObserverEntry(
        name="spy", observer_class=type("Spy", (), {
            "execute": lambda self, event: captured.append(event)})))
    yield captured
    clear()


def _execution(conn, job_id, status="running") -> str:
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status) "
                    "VALUES (%s, %s, 1, %s)", (execution_id, job_id, status))
    conn.commit()
    return execution_id


def _job_state(conn, message_id) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT job_state FROM message WHERE id = %s", (message_id,))
        state = cur.fetchone()["job_state"]
    conn.commit()
    return state


def _job(conn, job_id) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
        row = cur.fetchone()
    conn.commit()
    return row


def _unblock(conn, world, stuck, who="owner"):
    return routes.unblock(_Req(conn, world[who], params={
        "id": stuck["conversation_id"], "message_id": stuck["message_id"]}))


# --- the happy path -------------------------------------------------------

def test_a_paused_turn_with_no_session_unblocks(conn, world, stuck, seen):
    response = _unblock(conn, world, stuck)

    assert response.status == 200
    assert response.body["changed"] is True
    assert _job_state(conn, stuck["message_id"]) == "terminal"


def test_the_job_row_is_untouched(conn, world, stuck):
    """It does not resume the job: the PAUSED row stays for an operator to inspect."""
    before = _job(conn, stuck["job_id"])

    _unblock(conn, world, stuck)

    assert _job(conn, stuck["job_id"])["status"] == "PAUSED"
    assert _job(conn, stuck["job_id"])["session_id"] == before["session_id"]


def test_the_next_turn_claims_once_the_thread_is_free(conn, world, stuck):
    """§4.4 defers a later turn while the thread's newest user turn is non-terminal."""
    from agento.framework.events import ClaimVerdict, JobClaimBeforeEvent
    from agento.modules.conversation.src.hooks import ConversationOrderingObserver

    _, second_job, _ = service.submit_message(
        conn, conversation_id=stuck["conversation_id"], user_id=world["owner"].id,
        client_message_id="c2", content="drugie")

    def verdict(job_id):
        event = JobClaimBeforeEvent(job_id=job_id)
        with conn.cursor() as cur:
            event.cursor = cur
            ConversationOrderingObserver().execute(event)
        conn.commit()
        return event.verdict

    assert verdict(second_job) == ClaimVerdict.DEFER

    _unblock(conn, world, stuck)

    assert verdict(second_job) is not ClaimVerdict.DEFER


def test_the_event_carries_all_three_fields(conn, world, stuck, seen):
    _unblock(conn, world, stuck)

    assert len(seen) == 1
    assert seen[0].conversation_id == stuck["conversation_id"]
    assert seen[0].message_id == stuck["message_id"]
    assert seen[0].actor_id == world["owner"].id


# --- guard 1: only an unrecoverable turn ---------------------------------

def test_a_paused_job_with_a_session_is_refused(conn, world, stuck):
    """It can still resume. Marking it terminal would let the next turn run beside it."""
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET session_id = 'sesja-1' WHERE id = %s",
                    (stuck["job_id"],))
    conn.commit()

    response = _unblock(conn, world, stuck)

    assert response.status == 409
    assert _job_state(conn, stuck["message_id"]) == "published"


def test_a_running_job_is_refused(conn, world, stuck):
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'RUNNING' WHERE id = %s", (stuck["job_id"],))
    conn.commit()

    assert _unblock(conn, world, stuck).status == 409
    assert _job_state(conn, stuck["message_id"]) == "published"


def test_a_queued_job_is_refused(conn, world, stuck):
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'TODO' WHERE id = %s", (stuck["job_id"],))
    conn.commit()

    assert _unblock(conn, world, stuck).status == 409


def test_an_already_terminal_message_is_accepted_as_a_no_op(conn, world, stuck, seen):
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_state = 'terminal' WHERE id = %s",
                    (stuck["message_id"],))
    conn.commit()

    response = _unblock(conn, world, stuck)

    assert response.status == 200
    assert response.body["changed"] is False
    assert seen == []


# --- guard 2: the process must be gone -----------------------------------

def test_a_turn_whose_newest_execution_is_still_running_is_refused(conn, world, stuck):
    """PAUSED with no session also holds while a stopped process is still alive. Freeing
    the turn here would start the next one beside that process."""
    _execution(conn, stuck["job_id"], "running")

    assert _unblock(conn, world, stuck).status == 409
    assert _job_state(conn, stuck["message_id"]) == "published"


def test_the_same_turn_unblocks_once_the_execution_is_abandoned(conn, world, stuck):
    execution_id = _execution(conn, stuck["job_id"], "running")
    assert _unblock(conn, world, stuck).status == 409

    with conn.cursor() as cur:
        cur.execute("UPDATE execution SET status = 'abandoned' WHERE execution_id = %s",
                    (execution_id,))
    conn.commit()

    assert _unblock(conn, world, stuck).status == 200
    assert _job_state(conn, stuck["message_id"]) == "terminal"


def test_only_the_newest_execution_decides(conn, world, stuck):
    """An older attempt that was abandoned says nothing about the process running now."""
    _execution(conn, stuck["job_id"], "abandoned")
    _execution(conn, stuck["job_id"], "running")

    assert _unblock(conn, world, stuck).status == 409


def test_a_turn_with_no_execution_at_all_unblocks(conn, world, stuck):
    """The pause landed before any execution row existed; there is no process to wait for."""
    assert _unblock(conn, world, stuck).status == 200


# --- who may call it ------------------------------------------------------

def test_a_stranger_gets_404(conn, world, stuck):
    """404, never 403: a 403 would confirm the thread exists."""
    response = _unblock(conn, world, stuck, who="stranger")

    assert response.status == 404
    assert _job_state(conn, stuck["message_id"]) == "published"


def test_an_admin_may_free_someone_elses_thread(conn, world, stuck):
    assert _unblock(conn, world, stuck, who="admin").status == 200


def test_a_message_from_another_thread_is_404(conn, world, stuck):
    other = service.create_conversation(conn, user_id=world["owner"].id,
                                        agent_view_id=world["view"], title="t2")

    response = routes.unblock(_Req(conn, world["owner"], params={
        "id": other, "message_id": stuck["message_id"]}))

    assert response.status == 404
    assert _job_state(conn, stuck["message_id"]) == "published"


def test_an_assistant_row_cannot_be_unblocked(conn, world, stuck):
    """Only a user turn has a `job_state` at all."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO message (conversation_id, role, content) "
                    "VALUES (%s, 'assistant', 'odpowiedź')", (stuck["conversation_id"],))
        assistant_id = cur.lastrowid
    conn.commit()

    response = routes.unblock(_Req(conn, world["owner"], params={
        "id": stuck["conversation_id"], "message_id": assistant_id}))

    assert response.status == 404


# --- the panel sees the cause before the unblock -------------------------

def test_the_read_path_shows_a_stuck_turn_as_blocked_with_its_cause(conn, world, stuck):
    response = routes.messages(_Req(conn, world["owner"],
                                    params={"id": stuck["conversation_id"]}))

    turn = response.body[0]
    assert turn["blocked"] is True
    assert turn["blocked_reason"] == "paused_unrecoverable"


def test_a_resumable_paused_turn_reads_as_blocked_but_recoverable(conn, world, stuck):
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET session_id = 'sesja-1' WHERE id = %s",
                    (stuck["job_id"],))
    conn.commit()

    turn = routes.messages(_Req(conn, world["owner"],
                                params={"id": stuck["conversation_id"]})).body[0]

    assert turn["blocked"] is True
    assert turn["blocked_reason"] == "paused"


def test_an_ordinary_turn_is_not_blocked(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    service.submit_message(conn, conversation_id=conversation_id,
                           user_id=world["owner"].id, client_message_id="c1",
                           content="pytanie")

    turns = routes.messages(_Req(conn, world["owner"],
                                 params={"id": conversation_id})).body

    assert [t["blocked"] for t in turns] == [False]
    assert turns[0]["blocked_reason"] is None


def test_the_turn_is_no_longer_blocked_after_the_unblock(conn, world, stuck):
    _unblock(conn, world, stuck)

    turn = routes.messages(_Req(conn, world["owner"],
                                params={"id": stuck["conversation_id"]})).body[0]

    assert turn["blocked"] is False


def test_an_assistant_row_is_never_blocked(conn, world, stuck):
    """It has no job to be paused; the LEFT JOIN must not make it look stuck."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO message (conversation_id, role, content) "
                    "VALUES (%s, 'assistant', 'odpowiedź')", (stuck["conversation_id"],))
    conn.commit()

    rows = routes.messages(_Req(conn, world["owner"],
                                params={"id": stuck["conversation_id"]})).body

    assert [r["blocked"] for r in rows if r["role"] == "assistant"] == [False]


# --- this epic writes no admin_audit row ---------------------------------

def test_the_module_writes_no_sql_against_admin_audit():
    """Ordering, not a runtime branch: `admin_audit` is framework-owned (E7 §8.1/§14) and
    `conversation` declares no E7 dependency. E7 adds the call from `service.unblock()` to
    the framework's transaction-aware writer - never module SQL, never an observer, whose
    failure would be swallowed."""
    import subprocess
    from pathlib import Path

    found = subprocess.run(
        ["grep", "-rn", "admin_audit", "src/agento/modules/conversation/"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2])

    assert found.stdout == "", found.stdout


# --- one transaction, and the UPDATE decides ------------------------------

def test_a_second_unblock_changes_nothing_and_announces_nothing(conn, world, stuck, seen):
    """Two callers both pass the guards; only the one whose UPDATE matched a row wins.

    The guards are read and the turn is marked terminal in ONE transaction, and `changed`
    comes from that UPDATE's rowcount rather than from the read before it - otherwise both
    callers report a change they did not make and the event fires twice for one turn.
    """
    first = _unblock(conn, world, stuck)
    second = _unblock(conn, world, stuck)

    assert (first.body["changed"], second.body["changed"]) == (True, False)
    assert len(seen) == 1


def test_the_guards_and_the_update_are_one_transaction(conn, world, stuck):
    """A resume between the reads and the write is what the single transaction closes.

    The proof is structural: the commit that carries the `job_state` change is the only
    one in the call, so nothing it read can have been committed away before it lands.
    """
    commits = {"n": 0}
    real = conn.commit

    def counting_commit(*a, **kw):
        commits["n"] += 1
        return real(*a, **kw)

    conn.commit = counting_commit
    try:
        assert _unblock(conn, world, stuck).body["changed"] is True
    finally:
        conn.commit = real

    assert commits["n"] == 1
