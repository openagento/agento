from __future__ import annotations

import json
import logging

import pymysql

from .db import get_connection
from .event_manager import get_event_manager
from .events import JobPublishedEvent
from .job_models import AgentType, JobRequester


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

            # Dedupe on the unique idempotency_key with a SELECT instead of relying
            # on INSERT IGNORE: a rejected INSERT IGNORE still burns an auto_increment
            # id, so every duplicate publish grew the job id counter (AG-22). Checking
            # first keeps the counter flat when the row already exists.
            cur.execute(
                "SELECT id FROM job WHERE idempotency_key = %s LIMIT 1",
                (idempotency_key,),
            )
            if cur.fetchone() is not None:
                if logger:
                    logger.debug(f"Duplicate skipped: key={idempotency_key}")
                return False

            # requester is pure metadata - never part of idempotency_key or skip_if_active dedupe
            requester_key = requester.key if requester else None
            requester_email = requester.email if requester else None
            requester_trust = requester.trust.value if requester else "claimed"
            requester_meta = (
                json.dumps(requester.meta, allow_nan=False)  # fail loud on NaN/Inf before MySQL JSON rejects it
                if requester and requester.meta is not None    # preserve explicit {}, only None -> NULL
                else None
            )
            try:
                cur.execute(
                    """
                    INSERT INTO job
                        (type, source, agent_view_id, priority, reference_id,
                         idempotency_key, status, attempt, max_attempts,
                         requester_key, requester_email, requester_trust, requester_meta)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, 'TODO', 0, %s, %s, %s, %s, %s)
                    """,
                    (agent_type.value, source, agent_view_id, priority,
                     reference_id, idempotency_key, max_attempts,
                     requester_key, requester_email, requester_trust, requester_meta),
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
