"""`jira/resume_session` and `jira_periodic_tasks/resume_session` against MySQL.

Drives the real publisher, the policies registered through bootstrap and the real
`Consumer._try_dequeue`: with the field 0 the next turn on an issue stays in the issue's
thread but starts a fresh session; with 1 (the default) it resumes the previous turn.
"""
from __future__ import annotations

import logging

import pytest

from agento.framework.consumer import Consumer
from agento.framework.event_manager import get_event_manager
from agento.framework.job_models import AgentType
from agento.framework.publish_service import publish_job
from agento.modules.jira.src.session_policy import ResumeSessionPolicy
from agento.modules.jira_periodic_tasks.src.session_policy import PeriodicResumeSessionPolicy

from .conftest import _test_connection, bootstrap_for_tests


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
def view(conn) -> int:
    _sql(conn, "INSERT IGNORE INTO workspace (code, label) VALUES ('jr_ws', 't')")
    ws = _sql(conn, "SELECT id FROM workspace WHERE code = 'jr_ws'")[0]["id"]
    _sql(conn, "INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'jr_av', 't')",
         (ws,))
    return _sql(conn, "SELECT id FROM agent_view WHERE code = 'jr_av'")[0]["id"]


@pytest.fixture
def consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


_keys = iter(range(10**6))


def _publish(int_db_config, view, agent_type) -> int:
    return publish_job(source="jira", agent_type=agent_type, agent_view_id=view,
                       reference_id="AI-1", idempotency_key=f"jr:{next(_keys)}", requester=None,
                       priority=50, config=int_db_config)


def _set(conn, view, path, value) -> None:
    _sql(conn, "INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
               "VALUES ('agent_view', %s, %s, %s, 0)", (view, path, value))


def test_both_policies_are_reached_through_bootstrap():
    classes = {e.observer_class for e in get_event_manager()._observers.get("job_claim_before", [])}
    assert {ResumeSessionPolicy, PeriodicResumeSessionPolicy} <= classes


@pytest.mark.parametrize(("agent_type", "path"), [
    (AgentType.TODO, "jira/resume_session"),
    (AgentType.FOLLOWUP, "jira/resume_session"),
    (AgentType.CRON, "jira_periodic_tasks/resume_session"),
])
@pytest.mark.parametrize("value", [None, "1", "0"])
def test_the_field_decides_whether_the_next_turn_resumes_in_the_same_thread(
    conn, consumer, int_db_config, view, agent_type, path, value,
):
    first = _publish(int_db_config, view, agent_type)
    assert consumer._try_dequeue().id == first
    _sql(conn, "UPDATE job SET status = 'SUCCESS', session_id = 'issue-session', "
               "finished_at = NOW(6) WHERE id = %s", (first,))
    if value is not None:
        _set(conn, view, path, value)
    second = _publish(int_db_config, view, agent_type)

    job = consumer._try_dequeue()

    fresh = value == "0"
    assert job is not None and job.id == second
    rows = {r["id"]: r for r in _sql(conn, "SELECT id, thread_id, resume_job_id FROM job "
                                           "WHERE id IN (%s, %s)", (first, second))}
    assert rows[second]["thread_id"] == rows[first]["thread_id"]    # the issue's thread
    assert (job.fresh_session, job.thread_detached) == (fresh, False)
    assert rows[second]["resume_job_id"] == (None if fresh else first)
    with consumer._db() as c:
        assert Consumer._resume_session(c, job, job.agent_type) == (
            None if fresh else "issue-session")
