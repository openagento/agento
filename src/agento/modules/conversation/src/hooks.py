"""The ordering rule, as a claim-time veto (PRD E3-E5 §4.4).

One conversation runs one turn at a time. The rule lives here, in the module, and reaches
the queue through the framework's `job_claim_before` seam - the framework itself names no
conversation and reads no `conversation/*` config (PLC-2), and with this module disabled
there is no rule and every job is claimed as before.

The veto is checked at claim time rather than at publish time on purpose: at publish time
the earlier turn may still be running, and a job that was allowed to exist but must not run
yet is exactly what the queue is for.
"""
from __future__ import annotations

import uuid

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.events import ClaimVerdict, JobClaimBeforeEvent

from . import service
from .workflow import ReferenceUnusable, parse_reference

JOB_TYPE = "conversation"
# A turn that has not reached `terminal` is a turn this thread is still waiting on.
UNFINISHED = ("pending", "published")


class ConversationOrderingObserver:
    """Defer a conversation job while an earlier turn of its thread is unfinished."""

    def execute(self, event: object) -> None:
        if not isinstance(event, JobClaimBeforeEvent):
            return
        # ponytail: one connection per claim attempt on a conversation job. The claim
        # transaction is already open on another connection and this one only reads, so
        # sharing it would mean handing the observer the framework's cursor.
        conn = get_connection(DatabaseConfig.from_env())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT type, reference_id FROM job WHERE id = %s", (event.job_id,))
                job = cur.fetchone()
            if job is None or job["type"] != JOB_TYPE:
                return

            # A reference_id this module cannot read is not an ordering question: the
            # workflow will fail the job and the queue will dead-letter it. Deferring it
            # instead would hold it forever, invisibly.
            try:
                conversation_id, message_id = parse_reference(job["reference_id"])
            except ReferenceUnusable:
                return
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM message WHERE conversation_id = %s AND id < %s "
                    "AND job_state IN %s LIMIT 1",
                    (conversation_id, message_id, UNFINISHED),
                )
                blocked = cur.fetchone() is not None
            if blocked:
                event.verdict = ClaimVerdict.DEFER
                event.delay_ms = service.config(conn, "claim/defer_backoff_ms")
        finally:
            conn.close()


class ConversationExecutionIds:
    """Mints one `execution` row per attempt (PRD E3-E5 §5.1).

    The id is a UUID and not `{job_id}-{attempt}`: the pool-wait path refunds an attempt, so
    two real attempts of one job can carry the same number, and an id built from the pair
    would collide on the table's own unique key. The row is written on the framework's open
    connection and NOT committed here - it commits with the transition that produced it.
    """

    def mint(self, *, conn, job_id: int, attempt: int) -> str | None:
        execution_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO execution (execution_id, job_id, attempt, status) "
                "VALUES (%s, %s, %s, 'running')",
                (execution_id, job_id, attempt),
            )
        return execution_id


class ConversationResumeSessions:
    """The harness session a follow-up turn continues (PRD E3-E5 §5.2).

    Only for a follow-up. A retry (`attempt > 1`) is already resumed by the framework's own
    rule, which hands the runner the same job's `session_id` and an empty prompt - answering
    here as well would replace a resume of the interrupted run with a resume of the previous
    turn, and the interrupted turn's work would be lost.

    The session comes from the previous turn's JOB row. `execution.harness_session_id` is
    the eventual home for it, but nothing writes that column until the finalizer lands, and
    a resolver that reads an always-NULL column resumes nothing.
    """

    def resolve(self, *, conn, job_id: int, attempt: int) -> str | None:
        if attempt > 1:
            return None
        with conn.cursor() as cur:
            cur.execute("SELECT type, reference_id FROM job WHERE id = %s", (job_id,))
            job = cur.fetchone()
        if job is None or job["type"] != JOB_TYPE:
            return None
        try:
            conversation_id, message_id = parse_reference(job["reference_id"])
        except ReferenceUnusable:
            return None

        with conn.cursor() as cur:
            cur.execute(
                "SELECT j.session_id FROM message m JOIN job j ON j.id = m.job_id "
                "WHERE m.conversation_id = %s AND m.id < %s AND j.session_id IS NOT NULL "
                "ORDER BY m.id DESC LIMIT 1",
                (conversation_id, message_id),
            )
            row = cur.fetchone()
        return None if row is None else row["session_id"]
