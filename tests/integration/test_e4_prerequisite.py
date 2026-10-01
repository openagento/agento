"""The E4 slice gate (PRD E3-E5 §13, PRD:1061).

§13 makes E7 §4.3.1's `job_stop_request` table, its three acknowledging paths (the consumer
monitor, the pre-spawn status re-check, the stop-request pass) and E7 §8.1's `admin_audit`
migration **prerequisites of E4**, not later integrations. §4.5's unblock guard 2 refuses
while the turn's newest `execution` is still `running`, and nothing in this epic finalizes a
paused run: without the acknowledging paths the way out of §5.3's trap exists in code and is
unreachable in practice.

So this file is not "E4 complete except for E7". It is the gate, and **while these tests are
red the E4 slice is not done**, whatever the rest of the slice passes. Each one is written
against the contract E7 must deliver, so E7 makes them green without editing them.

The audit assertions are on the ROW, not on the migration (TST-1): a migration that ships an
empty table records nothing. And the audit write and the `message.job_state` change are one
transaction or the audit is a lie (§6.4.2, PRD:631-632) - the last test is what proves it.
"""
from __future__ import annotations

import uuid

import pytest

from agento.framework import job_store
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
def running(conn, world):
    """A turn whose job is RUNNING with a live execution - what an operator pauses."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")
    execution_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'RUNNING', session_id = NULL WHERE id = %s",
                    (job_id,))
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status) "
                    "VALUES (%s, %s, 1, 'running')", (execution_id, job_id))
    conn.commit()
    return {"conversation_id": conversation_id, "message_id": message_id,
            "job_id": job_id, "execution_id": execution_id}


def _rows(conn, sql, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _unblock(conn, world, thread, who="owner"):
    return routes.unblock(_Req(conn, world[who], params={
        "id": thread["conversation_id"], "message_id": thread["message_id"]}))


def _stop_request_pass(conn) -> None:
    """The consumer's stop-request pass (E7 §4.3.1): acknowledge each open stop request by
    finalizing its execution `abandoned`. E7 owns the real one; this calls it."""
    from agento.framework.consumer import run_stop_request_pass

    run_stop_request_pass(conn)


def _execution_status(conn, execution_id: str) -> str:
    return _rows(conn, "SELECT status FROM execution WHERE execution_id = %s",
                 (execution_id,))[0]["status"]


# --- the table -------------------------------------------------------------

def test_job_stop_request_table_exists(conn):
    """E7 §4.3.1's table, after `setup:upgrade` has run against this database."""
    columns = {r["Field"] for r in _rows(conn, "SHOW COLUMNS FROM job_stop_request")}

    assert {"id", "job_id", "execution_id", "requested_by", "requested_at",
            "acknowledged_at"} <= columns


# --- the acknowledging path ------------------------------------------------

def test_pause_writes_stop_request_and_pass_finalizes(conn, running):
    """A pause is a REQUEST, not a kill: it writes the row, and an acknowledging path
    finalizes the execution. Without that, guard 2 below never opens."""
    job_store.pause_job(conn, running["job_id"])

    requests = _rows(conn, "SELECT * FROM job_stop_request WHERE job_id = %s",
                     (running["job_id"],))
    assert len(requests) == 1
    assert requests[0]["execution_id"] == running["execution_id"]
    assert requests[0]["acknowledged_at"] is None

    _stop_request_pass(conn)

    assert _execution_status(conn, running["execution_id"]) == "abandoned"
    assert _rows(conn, "SELECT acknowledged_at FROM job_stop_request WHERE job_id = %s",
                 (running["job_id"],))[0]["acknowledged_at"] is not None


def test_unblock_after_acknowledgement(conn, world, running):
    """End to end: pause, acknowledge, then §4.5's guard 2 passes and the thread is free."""
    job_store.pause_job(conn, running["job_id"])
    _stop_request_pass(conn)

    response = _unblock(conn, world, running)

    assert response.status == 200
    assert response.body["changed"] is True
    assert _rows(conn, "SELECT job_state FROM message WHERE id = %s",
                 (running["message_id"],))[0]["job_state"] == "terminal"


# --- the audit row ---------------------------------------------------------

def test_unblock_writes_one_audit_row(conn, world, running):
    job_store.pause_job(conn, running["job_id"])
    _stop_request_pass(conn)

    assert _unblock(conn, world, running).status == 200

    audits = _rows(conn, "SELECT * FROM admin_audit WHERE action = 'conversation.unblock'")
    assert len(audits) == 1
    assert audits[0]["actor_id"] == world["owner"].id
    assert audits[0]["meta"] == {"conversation_id": running["conversation_id"],
                                 "message_id": running["message_id"]}


def test_refused_unblock_writes_no_audit_and_rollback_is_atomic(conn, world, running):
    """Two halves of one property. A refusal changed nothing, so it records nothing; and a
    failure after the audit insert must take the `job_state` change with it, or the audit
    claims a change that never happened."""
    # Guard 2 refuses while the execution is still running: nothing was freed, nothing logged.
    job_store.pause_job(conn, running["job_id"])

    assert _unblock(conn, world, running).status == 409
    assert _rows(conn, "SELECT id FROM admin_audit WHERE action = 'conversation.unblock'") == []

    # Now the same unblock, interrupted after the audit insert.
    _stop_request_pass(conn)
    real_commit = conn.commit
    calls = {"n": 0}

    def fail_on_the_commit_that_carries_both(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("the connection died mid-commit")

    conn.commit = fail_on_the_commit_that_carries_both
    try:
        with pytest.raises(RuntimeError):
            _unblock(conn, world, running)
    finally:
        conn.commit = real_commit
    conn.rollback()

    assert _rows(conn, "SELECT id FROM admin_audit WHERE action = 'conversation.unblock'") == []
    assert _rows(conn, "SELECT job_state FROM message WHERE id = %s",
                 (running["message_id"],))[0]["job_state"] == "published"
