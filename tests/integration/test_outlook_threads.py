"""One mail conversation = one thread, only while `outlook_list_thread` is on (thread_gate).

Drives the real publisher, the real `OutlookThreadGate` (registered through bootstrap) and the
real `Consumer._try_dequeue`. Revocation: the tool is turned off while jobs of a mail thread
wait; each such job then runs detached, as if the tool had been off from the start.
"""
from __future__ import annotations

import logging

import pytest

from agento.framework.consumer import Consumer
from agento.framework.event_manager import get_event_manager
from agento.framework.job_models import AgentType
from agento.framework.publisher import insert_job
from agento.modules.outlook.src.channel import OutlookPublisher
from agento.modules.outlook.src.thread_gate import OutlookThreadGate, conversation_ref, thread_read_on

from .conftest import _test_connection, bootstrap_for_tests

SENDER = "klient@example.com"
TOOL = "tools/outlook_list_thread/is_enabled"


@pytest.fixture(autouse=True)
def _registered():
    bootstrap_for_tests()


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    yield c
    c.close()


def _sql(conn, sql, params=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


@pytest.fixture
def views(conn) -> tuple[int, int]:
    _sql(conn, "INSERT IGNORE INTO workspace (code, label) VALUES ('ot_ws', 't')")
    ws = _sql(conn, "SELECT id FROM workspace WHERE code = 'ot_ws'")[0]["id"]
    for code in ("ot_mail", "ot_other"):
        _sql(conn, "INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, %s, 't')", (ws, code))
    rows = _sql(conn, "SELECT id, code FROM agent_view WHERE code IN ('ot_mail', 'ot_other')")
    by_code = {r["code"]: r["id"] for r in rows}
    return by_code["ot_mail"], by_code["ot_other"]


@pytest.fixture
def consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


def _thread_read(conn, view: int, on: bool) -> None:
    _sql(conn, "DELETE FROM core_config_data WHERE scope = 'agent_view' AND scope_id = %s AND path = %s",
         (view, TOOL))
    _sql(conn, "INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
               "VALUES ('agent_view', %s, %s, %s, 0)", (view, TOOL, "1" if on else "0"))


def _mail(conn, int_db_config, view: int, message_id: str, conversation_id: str = "conv-1") -> int:
    """Publish one admitted message the way the poller does: the conversation is passed only
    while thread read is on for the view."""
    joined = conversation_id if thread_read_on(conn, view) else None
    conn.commit()                       # the config read opened a snapshot
    assert OutlookPublisher().publish_mail(
        int_db_config, message_id, agent_view_id=view, sender_email=SENDER, dmarc="pass",
        allowed_senders=[SENDER], subject="Faktura", conversation_id=joined,
    )
    return _sql(conn, "SELECT id FROM job WHERE idempotency_key = %s", (f"outlook:mail:{message_id}",))[0]["id"]


def _row(conn, job_id) -> dict:
    return _sql(conn, "SELECT * FROM job WHERE id = %s", (job_id,))[0]


def _ref(conn, job_id) -> str:
    return _sql(conn, "SELECT t.ref FROM job j JOIN thread t ON t.id = j.thread_id WHERE j.id = %s",
                (job_id,))[0]["ref"]


def _finish(conn, job_id, session_id="s") -> None:
    _sql(conn, "UPDATE job SET status = 'SUCCESS', session_id = %s, finished_at = NOW(6) WHERE id = %s",
         (session_id, job_id))


def _insert(int_db_config, **fields) -> int:
    from agento.framework.db import get_connection

    c = get_connection(int_db_config)
    try:
        with c.cursor() as cur:
            job_id = insert_job(cur, priority=50, max_attempts=3, requester=None, **{
                "agent_type": AgentType.FOLLOWUP, "source": "outlook", "reference_id": "faktura::m1",
                **fields})
        c.commit()
    finally:
        c.close()
    return job_id


def test_the_gate_is_reached_through_bootstrap():
    observers = get_event_manager()._observers.get("job_claim_before", [])
    assert OutlookThreadGate in [o.observer_class for o in observers]


@pytest.mark.parametrize("on", [True, False])
def test_two_messages_of_one_conversation_share_a_thread_only_with_thread_read_on(
    conn, int_db_config, views, on,
):
    view, _ = views
    _thread_read(conn, view, on)

    first, second = _mail(conn, int_db_config, view, "m1"), _mail(conn, int_db_config, view, "m2")

    same = _row(conn, first)["thread_id"] == _row(conn, second)["thread_id"]
    assert same is on
    if on:
        assert _ref(conn, first) == conversation_ref("conv-1")


def test_with_thread_read_on_the_next_message_continues_the_conversation(
    conn, consumer, int_db_config, views,
):
    view, _ = views
    _thread_read(conn, view, True)
    first = _mail(conn, int_db_config, view, "m1")
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="mail-session")
    second = _mail(conn, int_db_config, view, "m2")

    job = consumer._try_dequeue()

    assert (job.id, job.thread_detached, job.resume_job_id) == (second, False, first)


def _revoked_thread(conn, consumer, int_db_config, view) -> tuple[int, int]:
    """A finished first turn of a mail thread (with a session); returns (first, thread_id)
    after the operator turns thread read off."""
    _thread_read(conn, view, True)
    first = _mail(conn, int_db_config, view, "m1")
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="mail-session")
    return first, _row(conn, first)["thread_id"]


def _assert_detached(conn, job, job_id, thread_id) -> None:
    assert job is not None and job.id == job_id
    assert job.thread_detached and job.resume_job_id is None and job.session_id is None
    row = _row(conn, job_id)
    assert row["thread_id"] != thread_id and row["resume_job_id"] is None and row["session_id"] is None


def test_a_queued_message_runs_detached_after_revocation(conn, consumer, int_db_config, views):
    view, _ = views
    _thread_read(conn, view, True)
    first = _mail(conn, int_db_config, view, "m1")
    assert consumer._try_dequeue().id == first
    _finish(conn, first, session_id="mail-session")
    second = _mail(conn, int_db_config, view, "m2")
    thread_id = _row(conn, second)["thread_id"]
    assert thread_id == _row(conn, first)["thread_id"]
    _thread_read(conn, view, False)

    _assert_detached(conn, consumer._try_dequeue(), second, thread_id)
    assert _ref(conn, second) == f"outlook:{_row(conn, second)['reference_id']}"   # its own thread


@pytest.mark.parametrize("kind", ["followup", "wake"])
def test_a_queued_followup_or_wake_runs_detached_after_revocation(
    conn, consumer, int_db_config, views, kind,
):
    """Both are depth-0 `followup` rows that copy the thread (schedule_followup, the wake)."""
    view, _ = views
    first, thread_id = _revoked_thread(conn, consumer, int_db_config, view)
    later = _insert(int_db_config, agent_view_id=view, thread_id=thread_id,
                    idempotency_key=f"{kind}:{first}", context="check again")
    _thread_read(conn, view, False)

    _assert_detached(conn, consumer._try_dequeue(), later, thread_id)


def test_a_queued_child_in_another_view_runs_detached_after_revocation(
    conn, consumer, int_db_config, views,
):
    """Keyed on the thread's view (the mailbox's), not the child's: the child's own view has
    thread read on, the mail thread's view has it off."""
    view, other = views
    first, thread_id = _revoked_thread(conn, consumer, int_db_config, view)
    _thread_read(conn, other, True)
    child = _insert(int_db_config, agent_type=AgentType.TODO, source="subagent", reference_id=str(first),
                    agent_view_id=other, thread_id=thread_id, parent_job_id=first, depth=1,
                    idempotency_key=f"subagent:{first}:1")
    _thread_read(conn, view, False)

    _assert_detached(conn, consumer._try_dequeue(), child, thread_id)


def test_a_retry_after_revocation_clears_its_own_session(conn, consumer, int_db_config, views):
    view, _ = views
    first, thread_id = _revoked_thread(conn, consumer, int_db_config, view)
    second = _mail(conn, int_db_config, view, "m2")
    assert consumer._try_dequeue().resume_job_id == first           # attempt 1 continues the thread
    # Attempt 1 fails and is retried, with its own session on the row.
    _sql(conn, "UPDATE job SET status = 'TODO', session_id = 'attempt-1', scheduled_after = NOW() "
               "WHERE id = %s", (second,))
    _thread_read(conn, view, False)

    job = consumer._try_dequeue()

    assert job.attempt == 2
    _assert_detached(conn, job, second, thread_id)


def test_a_child_continuation_after_revocation_starts_fresh_away_from_the_old_children(
    conn, consumer, int_db_config, views,
):
    """A continuation of child X (line X) whose line has a session and finished children of its
    own. Detached, it resumes nothing and is no longer in the thread of X and X's children, so
    no line or thread rule reaches them from it."""
    view, _ = views
    first, thread_id = _revoked_thread(conn, consumer, int_db_config, view)
    child = dict(agent_type=AgentType.TODO, source="subagent", reference_id=str(first),
                 agent_view_id=view, thread_id=thread_id)
    x = _insert(int_db_config, **child, parent_job_id=first, depth=1, idempotency_key="x")
    _finish(conn, x, session_id="child-session")
    grandchild = _insert(int_db_config, **child, parent_job_id=x, depth=2, idempotency_key="g")
    _finish(conn, grandchild, session_id="grandchild-session")
    continuation = _insert(int_db_config, **child, parent_job_id=first, depth=1, line_id=x,
                           idempotency_key="x-again")
    _thread_read(conn, view, False)

    _assert_detached(conn, consumer._try_dequeue(), continuation, thread_id)
    # `agent_result` reads a row only through its caller: the old children are X's, not the
    # continuation's, and they stay in the mail thread it has left.
    assert _sql(conn, "SELECT id FROM job WHERE parent_job_id = %s", (continuation,)) == []
    assert {_row(conn, j)["thread_id"] for j in (x, grandchild)} == {thread_id}
