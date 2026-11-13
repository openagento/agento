"""Publishing that a caller can build a conversation turn on (PRD E3-E5 §4.3).

`publisher.publish()` answers "did I insert it?". A submission needs two more things:
the **id** of the job its message now waits on, and a `job.queued` outbox row that
commits with the insert (§6.4.2) — an announced job that does not exist, or a job nothing
announced, are both states a relay cannot repair. Both entry points share one INSERT
(`publisher.insert_job`), so there is still exactly one way a job row is born.
"""
from __future__ import annotations

import pymysql

from .database_config import DatabaseConfig
from .db import get_connection
from .event_manager import get_event_manager
from .events import JobPublishedEvent
from .job_models import JobRequester
from .job_types import JobTypeLike
from .outbox import write_outbox
from .publisher import existing_job_id, insert_job


def publish_job_on(
    cur,
    *,
    source: str,
    agent_type: JobTypeLike,
    agent_view_id: int | None,
    reference_id: str | None,
    idempotency_key: str,
    requester: JobRequester | None,
    priority: int,
    prompt: str | None = None,
    context: str | None = None,
    max_attempts: int = 3,
    thread: str | None = None,
    thread_id: int | None = None,
    parent_job_id: int | None = None,
    depth: int = 0,
    line_id: int | None = None,
    test_config: dict | None = None,
    harness: str | None = None,
    model: str | None = None,
    schedule_id: int | None = None,
) -> tuple[int, JobPublishedEvent | None]:
    """Insert a job and its `job.queued` outbox row in the CALLER's transaction.

    Returns the job id and the `job_publish_after` payload, which the caller dispatches
    after it commits (EVT-4: a rollback must emit nothing). A key that already holds a job
    returns that job's id and None. It never commits or rolls back: a caller that holds a
    row lock (the schedule tick) keeps it. A race on the key raises IntegrityError.
    `harness`/`model` pin the job's runtime (docs/cli/job-runtime.md).
    """
    existing = existing_job_id(cur, idempotency_key)
    if existing is not None:
        return existing, None
    job_id = insert_job(
        cur,
        agent_type=agent_type,
        source=source,
        agent_view_id=agent_view_id,
        priority=priority,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        requester=requester,
        prompt=prompt,
        context=context,
        harness=harness,
        model=model,
        thread=thread,
        thread_id=thread_id,
        parent_job_id=parent_job_id,
        depth=depth,
        line_id=line_id,
        test_config=test_config,
        schedule_id=schedule_id,
    )
    write_outbox(
        cur,
        job_id=job_id,
        kind="job.queued",
        payload={
            "type": agent_type.value,
            "source": source,
            "agent_view_id": agent_view_id,
            "priority": priority,
        },
    )
    return job_id, JobPublishedEvent(
        type=agent_type.value,
        source=source,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        agent_view_id=agent_view_id,
        priority=priority,
        requester=requester,
        job_id=job_id,
    )


def publish_job(
    *,
    source: str,
    agent_type: JobTypeLike,
    agent_view_id: int | None,
    reference_id: str | None,
    idempotency_key: str,
    requester: JobRequester | None,
    priority: int,
    prompt: str | None = None,
    context: str | None = None,
    max_attempts: int = 3,
    config: object | None = None,
    thread: str | None = None,
    thread_id: int | None = None,
    parent_job_id: int | None = None,
    depth: int = 0,
    line_id: int | None = None,
    test_config: dict | None = None,
    harness: str | None = None,
    model: str | None = None,
    schedule_id: int | None = None,
) -> int:
    """Insert a job (or find the one this key already published) and return its id.

    `reference_id` is set in the insert, which is what makes the §6.4.1 relay's
    resolution of it race-free: the row a relay reads is never half-written.
    `thread` .. `line_id` place the job in its thread and line (`publisher.insert_job`).
    `test_config` is copied from the job this one is made from (a test run stays one).
    Its own transaction around `publish_job_on`.
    """
    conn = get_connection(config if config is not None else DatabaseConfig.from_env())
    try:
        with conn.cursor() as cur:
            try:
                job_id, event = publish_job_on(
                    cur,
                    source=source,
                    agent_type=agent_type,
                    agent_view_id=agent_view_id,
                    reference_id=reference_id,
                    idempotency_key=idempotency_key,
                    requester=requester,
                    priority=priority,
                    prompt=prompt,
                    context=context,
                    max_attempts=max_attempts,
                    thread=thread,
                    thread_id=thread_id,
                    parent_job_id=parent_job_id,
                    depth=depth,
                    line_id=line_id,
                    test_config=test_config,
                    harness=harness,
                    model=model,
                    schedule_id=schedule_id,
                )
            except pymysql.err.IntegrityError:
                # Race: another caller took the key between the SELECT and the INSERT.
                # The loser answers with the winner's id - a duplicate submission must
                # resolve to one job, not to an error.
                conn.rollback()
                winner = existing_job_id(cur, idempotency_key)
                if winner is None:
                    raise
                return winner
            if event is None:
                conn.rollback()  # the SELECT opened a transaction; do not leave it idle
                return job_id
            conn.commit()

        get_event_manager().dispatch("job_publish_after", event)
        return job_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
