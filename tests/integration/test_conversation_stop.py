"""Stop of a thread's running turn from the panel: the turn ends, the session stays.

The job comes from the thread on the server: the routes take no job id from the caller.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agento.modules.conversation.src import routes, service

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _Req


@pytest.fixture
def turn(conn, world):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    _, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'RUNNING', session_id = 'sesja-1', "
                    "runner_ref = 'runner-1' WHERE id = %s", (job_id,))
        cur.execute("INSERT INTO execution (execution_id, job_id, attempt, status, conversation_id) "
                    "VALUES (%s, %s, 1, 'running', %s)", (f"x-{job_id}", job_id, conversation_id))
    conn.commit()
    return {"conversation_id": conversation_id, "job_id": job_id}


def _stop(conn, world, turn, who="owner"):
    return routes.stop(_Req(conn, world[who], params={"id": turn["conversation_id"]}))


def _row(conn, sql, arg) -> dict:
    with conn.cursor() as cur:
        cur.execute(sql, (arg,))
        row = cur.fetchone()
    conn.commit()
    return row


def test_stop_ends_the_turn_and_frees_the_thread(conn, world, turn):
    with patch("agento.framework.job_store.runner_client.signal", return_value=True) as sig, \
            patch("agento.framework.runner.client.alive", return_value="dead"):
        response = _stop(conn, world, turn)
    assert response.status == 200
    assert response.body == {"job_id": turn["job_id"], "status": "DEAD"}
    assert sig.call_args.args[:2] == ("runner-1", f"job:{turn['job_id']}")
    job = _row(conn, "SELECT status, session_id, error_class FROM job WHERE id = %s", turn["job_id"])
    # The session stays: the next message resumes it (threads.resolve_resume).
    assert job == {"status": "DEAD", "session_id": "sesja-1", "error_class": service.STOPPED}
    assert _row(conn, "SELECT job_state FROM message WHERE job_id = %s",
                turn["job_id"])["job_state"] == "terminal"
    shown = routes.messages(_Req(conn, world["owner"], params={"id": turn["conversation_id"]}))
    assert shown.body[0]["stopped"] is True


def test_a_run_still_alive_keeps_the_turn_and_a_second_stop_ends_it(conn, world, turn):
    with patch("agento.framework.job_store.runner_client.signal", return_value=True), \
            patch("agento.framework.runner.client.alive", return_value="alive"), \
            patch("agento.framework.job_store.time.sleep"):
        assert _stop(conn, world, turn).status == 409
    assert _row(conn, "SELECT status FROM job WHERE id = %s", turn["job_id"])["status"] == "PAUSED"
    with patch("agento.framework.runner.client.alive", return_value="dead"):
        assert _stop(conn, world, turn).status == 200
    assert _row(conn, "SELECT status FROM job WHERE id = %s", turn["job_id"])["status"] == "DEAD"


def test_stop_with_no_running_turn_is_409(conn, world, turn):
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET status = 'SUCCESS' WHERE id = %s", (turn["job_id"],))
    conn.commit()
    assert _stop(conn, world, turn).status == 409


def test_a_stranger_gets_404_and_the_job_runs_on(conn, world, turn):
    assert _stop(conn, world, turn, who="stranger").status == 404
    assert _row(conn, "SELECT status FROM job WHERE id = %s", turn["job_id"])["status"] == "RUNNING"


def test_a_turn_with_no_user_message_stops_too(conn, world, turn):
    """A channel (Jira, a schedule) starts a turn with no message row: Stop reads the runs."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM message WHERE job_id = %s", (turn["job_id"],))
    conn.commit()
    shown = routes.show(_Req(conn, world["owner"], params={"id": turn["conversation_id"]}))
    assert shown.body["stoppable"] is True
    with patch("agento.framework.job_store.runner_client.signal", return_value=True), \
            patch("agento.framework.runner.client.alive", return_value="dead"):
        assert _stop(conn, world, turn).status == 200
    assert _row(conn, "SELECT status FROM job WHERE id = %s", turn["job_id"])["status"] == "DEAD"
    shown = routes.show(_Req(conn, world["owner"], params={"id": turn["conversation_id"]}))
    assert shown.body["stoppable"] is False


def test_a_sub_agent_job_is_not_the_threads_turn(conn, world, turn):
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET depth = 1 WHERE id = %s", (turn["job_id"],))
    conn.commit()
    assert _stop(conn, world, turn).status == 409
