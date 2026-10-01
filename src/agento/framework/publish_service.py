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
    max_attempts: int = 3,
    config: object | None = None,
) -> int:
    """Insert a job (or find the one this key already published) and return its id.

    `reference_id` is set in the insert, which is what makes the §6.4.1 relay's
    resolution of it race-free: the row a relay reads is never half-written.
    """
    conn = get_connection(config if config is not None else DatabaseConfig.from_env())
    try:
        with conn.cursor() as cur:
            existing = existing_job_id(cur, idempotency_key)
            if existing is not None:
                conn.rollback()  # the SELECT opened a transaction; do not leave it idle
                return existing

            try:
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
            except pymysql.err.IntegrityError:
                # Race: another caller took the key between the SELECT and the INSERT.
                # The loser answers with the winner's id - a duplicate submission must
                # resolve to one job, not to an error.
                conn.rollback()
                winner = existing_job_id(cur, idempotency_key)
                if winner is None:
                    raise
                return winner
            conn.commit()

        get_event_manager().dispatch(
            "job_publish_after",
            JobPublishedEvent(
                type=agent_type.value,
                source=source,
                reference_id=reference_id,
                idempotency_key=idempotency_key,
                agent_view_id=agent_view_id,
                priority=priority,
                requester=requester,
                job_id=job_id,
            ),
        )
        return job_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
