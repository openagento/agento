from __future__ import annotations

import json
import logging

import pymysql

from .db import get_connection
from .event_manager import get_event_manager
from .events import JobPublishedEvent
from .job_models import AgentType, JobRequester
from .job_types import JobTypeLike


def existing_job_id(cur, idempotency_key: str) -> int | None:
    """The id of the job already holding this key, or None.

    Dedupe on the unique idempotency_key with a SELECT instead of relying on
    INSERT IGNORE: a rejected INSERT IGNORE still burns an auto_increment id, so every
    duplicate publish grew the job id counter (AG-22). Checking first keeps it flat.
    """
    cur.execute("SELECT id FROM job WHERE idempotency_key = %s LIMIT 1", (idempotency_key,))
    row = cur.fetchone()
    return None if row is None else row["id"]


def insert_job(
    cur,
    *,
    agent_type: JobTypeLike,
    source: str,
    agent_view_id: int | None,
    priority: int,
    reference_id: str | None,
    idempotency_key: str,
    max_attempts: int,
    requester: JobRequester | None,
    prompt: str | None = None,
    context: str | None = None,
) -> int:
    """The single INSERT both entry points use. Returns the new job id.

    It does not commit: the caller owns the transaction, which is what lets
    `publish_service` put the outbox row in it.
    """
    # requester is pure metadata - never part of idempotency_key or skip_if_active dedupe
    requester_meta = (
        json.dumps(requester.meta, allow_nan=False)  # fail loud on NaN/Inf before MySQL JSON rejects it
        if requester and requester.meta is not None    # preserve explicit {}, only None -> NULL
        else None
    )
    cur.execute(
        """
        INSERT INTO job
            (type, source, agent_view_id, priority, reference_id,
             idempotency_key, status, attempt, max_attempts, prompt, context,
             requester_key, requester_email, requester_trust, requester_meta)
        VALUES
            (%s, %s, %s, %s, %s, %s, 'TODO', 0, %s, %s, %s, %s, %s, %s, %s)
        """,
        (agent_type.value, source, agent_view_id, priority, reference_id,
         idempotency_key, max_attempts, prompt, context,
         requester.key if requester else None,
         requester.email if requester else None,
         requester.trust.value if requester else "claimed",
         requester_meta),
    )
    return cur.lastrowid



def publish(
    config: object,
    agent_type: AgentType,
    source: str,
    idempotency_key: str,
    reference_id: str | None = None,
    max_attempts: int = 3,
    logger: logging.Logger | None = None,
    agent_view_id: int | None = None,
    priority: int = 50,
    skip_if_active: bool = False,
    requester: JobRequester | None = None,
) -> bool:
    """Insert a job into the queue. Returns True if inserted, False if duplicate.

    When ``skip_if_active`` is True and ``reference_id`` is set, the publish is
    skipped if a non-terminal job already exists for the same
    (type, source, agent_view_id, reference_id). Use this when the idempotency
    key rotates on every remote update (e.g. Jira `updated` timestamp), so a
    source-side search-index lag can't produce a duplicate enqueue while the
    original job is still TODO/RUNNING/PAUSED.
    """
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            if skip_if_active and reference_id is not None:
                cur.execute(
                    """
                    SELECT 1 FROM job
                    WHERE type = %s AND source = %s
                      AND agent_view_id <=> %s AND reference_id = %s
                      AND status IN ('TODO','RUNNING','PAUSED')
                    LIMIT 1
                    """,
                    (agent_type.value, source, agent_view_id, reference_id),
                )
                if cur.fetchone() is not None:
                    if logger:
                        logger.debug(
                            f"Active job exists, skipping: "
                            f"type={agent_type.value} source={source} "
                            f"ref={reference_id} agent_view_id={agent_view_id}"
                        )
                    return False

            if existing_job_id(cur, idempotency_key) is not None:
                if logger:
                    logger.debug(f"Duplicate skipped: key={idempotency_key}")
                return False

            try:
                insert_job(
                    cur,
                    agent_type=agent_type,
                    source=source,
                    agent_view_id=agent_view_id,
                    priority=priority,
                    reference_id=reference_id,
                    idempotency_key=idempotency_key,
                    max_attempts=max_attempts,
                    requester=requester,
                )
            except pymysql.err.IntegrityError:
                # Race: another publisher inserted the same idempotency_key between
                # our SELECT and this INSERT. The unique key rejects it - treat as a
                # duplicate, not an error.
                conn.rollback()
                if logger:
                    logger.debug(f"Duplicate skipped (race): key={idempotency_key}")
                return False
            conn.commit()
            inserted = cur.rowcount > 0

        if logger:
            if inserted:
                logger.info(
                    f"Published job: type={agent_type.value} source={source} "
                    f"ref={reference_id} key={idempotency_key} "
                    f"agent_view_id={agent_view_id} priority={priority}"
                )
            else:
                logger.debug(f"Duplicate skipped: key={idempotency_key}")

        if inserted:
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
                ),
            )

        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
