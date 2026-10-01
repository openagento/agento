"""The execution-id seam against a real database (PRD E3-E5 §5.1).

The point of the seam is attribution: every capability a run mints, and therefore every
tool call it makes, carries the id of the attempt that made it.
"""
from __future__ import annotations

import logging

import pytest

from agento.framework.consumer import Consumer
from agento.framework.execution_hooks import clear as clear_hooks
from agento.framework.execution_hooks import (
    mint_execution_id,
    register_execution_id_provider,
)
from agento.framework.job_types import clear_job_types, register_job_type
from agento.modules.conversation.src.hooks import ConversationExecutionIds

from .conftest import _test_connection, bootstrap_for_tests, fetch_job


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    with c.cursor() as cur:
        for table in ("execution", "toolbox_capability", "job"):
            cur.execute(f"DELETE FROM {table}")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def provider():
    clear_hooks()
    register_execution_id_provider(ConversationExecutionIds(), module="conversation")
    yield
    clear_hooks()


def _job(conn, key="exec:1") -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, reference_id, idempotency_key, status) "
                    "VALUES ('blank', 'test', 'E-1', %s, 'TODO')", (key,))
        job_id = cur.lastrowid
    conn.commit()
    return job_id


def _rows(conn, sql, params=()) -> list[dict]:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def test_the_provider_writes_one_execution_row_per_attempt(conn, provider):
    job_id = _job(conn)

    first = mint_execution_id(conn=conn, job_id=job_id, attempt=1)
    second = mint_execution_id(conn=conn, job_id=job_id, attempt=2)
    conn.commit()

    rows = _rows(conn, "SELECT execution_id, attempt, status FROM execution "
                       "WHERE job_id = %s ORDER BY attempt", (job_id,))
    assert [r["attempt"] for r in rows] == [1, 2]
    assert [r["execution_id"] for r in rows] == [first, second]
    assert {r["status"] for r in rows} == {"running"}


def test_two_attempts_with_one_number_do_not_collide(conn, provider):
    """The pool-wait path refunds an attempt, so `(job_id, attempt)` is not unique."""
    job_id = _job(conn)

    first = mint_execution_id(conn=conn, job_id=job_id, attempt=1)
    second = mint_execution_id(conn=conn, job_id=job_id, attempt=1)
    conn.commit()

    assert first != second
    assert len(_rows(conn, "SELECT id FROM execution WHERE job_id = %s", (job_id,))) == 2


def test_the_row_is_not_committed_by_the_provider(conn, provider):
    """It commits with the transition that produced it, never on its own."""
    job_id = _job(conn)

    mint_execution_id(conn=conn, job_id=job_id, attempt=1)
    conn.rollback()

    assert _rows(conn, "SELECT id FROM execution WHERE job_id = %s", (job_id,)) == []


def test_with_no_provider_nothing_is_written(conn):
    clear_hooks()
    job_id = _job(conn)

    assert mint_execution_id(conn=conn, job_id=job_id, attempt=1) is None
    assert _rows(conn, "SELECT id FROM execution") == []


def test_a_run_stamps_its_execution_id_on_the_capability_it_mints(
    conn, provider, int_db_config, int_consumer_config, mock_claude, int_agent_view
):
    """The seam's whole purpose, end to end: the run's capability is attributable."""
    bootstrap_for_tests()
    register_job_type("conversation", module="conversation")
    clear_hooks()
    register_execution_id_provider(ConversationExecutionIds(), module="conversation")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO job (type, source, reference_id, idempotency_key, status, "
                "agent_view_id) VALUES ('blank', 'jira', 'E-2', 'exec:run', 'TODO', %s)",
                (int_agent_view,),
            )
            job_id = cur.lastrowid
        conn.commit()

        consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
        job = consumer._try_dequeue()
        assert job is not None and job.id == job_id
        consumer._execute_job(job)
    finally:
        clear_job_types()

    assert fetch_job(job_id)["status"] == "SUCCESS"
    executions = _rows(conn, "SELECT execution_id FROM execution WHERE job_id = %s", (job_id,))
    capabilities = _rows(conn, "SELECT execution_id FROM toolbox_capability "
                               "WHERE job_id = %s", (job_id,))
    assert len(executions) == 1
    assert capabilities and {c["execution_id"] for c in capabilities} == {
        executions[0]["execution_id"]}
