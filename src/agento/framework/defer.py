"""Deferred claims and the stretches that collapse them (PRD E3-E5 §4.4, §10.1).

A job held behind an ordering rule is offered and refused once per poll tick. Announcing
each refusal would put hundreds of identical events in front of whoever is watching, so the
refusals are collapsed into a **stretch** - one row, opened at the first deferral, bumped at
every later one, and announced **once, when it closes**, carrying the final count.

The framework names no module and reads no module config here (PLC-2): the delay arrives in
the verdict, and all the framework does is clamp it.
"""
from __future__ import annotations

from .events import JobDeferAfterEvent
from .outbox import write_outbox

# Framework-owned bounds. A 0 from any observer would leave the row immediately eligible and
# restore the busy-loop the deferral exists to remove; a very large one would strand the job
# past any sensible poll interval. This is clamping, not validation: a claim must always end
# with a usable delay, never with an error.
DEFER_FLOOR_MS = 250
DEFER_CEILING_MS = 30_000


def clamp_delay(delay_ms: object) -> int:
    try:
        value = int(delay_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFER_FLOOR_MS
    return min(max(value, DEFER_FLOOR_MS), DEFER_CEILING_MS)


def defer_seconds(delay_ms: object) -> int:
    """The clamped delay in whole seconds, rounded UP - never zero.

    `job.scheduled_after` is a second-precision TIMESTAMP, so a sub-second delay would
    store as no delay at all and restore the busy loop the deferral exists to remove. The
    verdict speaks in milliseconds because that is what an observer can reason about; the
    queue's resolution is one second, and rounding up is the only direction that keeps the
    guarantee.
    """
    return -(-clamp_delay(delay_ms) // 1000)


def open_or_bump_stretch(cur, *, job_id: int) -> tuple[int, int]:
    """Open this job's stretch, or count one more deferral on the open one.

    Returns `(stretch_seq, defer_count)`. Runs on the CALLER's cursor, inside the claim
    transaction - a count that committed while the deferral rolled back would announce a
    block that never happened.
    """
    cur.execute(
        "SELECT id, stretch_seq, defer_count FROM job_defer_stretch "
        "WHERE job_id = %s AND closed_at IS NULL ORDER BY stretch_seq DESC LIMIT 1 FOR UPDATE",
        (job_id,),
    )
    row = cur.fetchone()
    if row is not None:
        cur.execute(
            "UPDATE job_defer_stretch SET defer_count = defer_count + 1 WHERE id = %s",
            (row["id"],),
        )
        return row["stretch_seq"], row["defer_count"] + 1

    cur.execute(
        "SELECT COALESCE(MAX(stretch_seq), 0) + 1 AS next FROM job_defer_stretch WHERE job_id = %s",
        (job_id,),
    )
    stretch_seq = cur.fetchone()["next"]
    cur.execute(
        "INSERT INTO job_defer_stretch (job_id, stretch_seq, defer_count) VALUES (%s, %s, 1)",
        (job_id, stretch_seq),
    )
    return stretch_seq, 1


def close_stretch(cur, *, job_id: int, reason: str = "claimed") -> JobDeferAfterEvent | None:
    """Close this job's open stretch and announce it. The event, or None if it was already closed.

    The close is a compare-and-set, and **only the caller that changed the row** writes the
    event: a concurrent claim and a replay both reach this, and one stretch is one
    announcement.
    """
    cur.execute(
        "SELECT id, stretch_seq, defer_count FROM job_defer_stretch "
        "WHERE job_id = %s AND closed_at IS NULL ORDER BY stretch_seq DESC LIMIT 1",
        (job_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    cur.execute(
        "UPDATE job_defer_stretch SET closed_at = NOW() WHERE id = %s AND closed_at IS NULL",
        (row["id"],),
    )
    if cur.rowcount != 1:
        return None

    write_outbox(
        cur,
        job_id=job_id,
        kind="job.deferred",
        payload={"stretch_seq": row["stretch_seq"], "defer_count": row["defer_count"],
                 "reason": reason},
    )
    # Returned, not dispatched: the caller owns the transaction, and an in-process observer
    # must not see a block that a rollback un-announces.
    return JobDeferAfterEvent(job_id=job_id, stretch_seq=row["stretch_seq"],
                              defer_count=row["defer_count"], reason=reason)


def prune_defer_stretches(conn, *, retention_days: int | None = None) -> int:
    """Delete closed stretches past the retention window. An OPEN row is never deleted.

    Whatever its age: a conversation blocked longer than the window must not have its live
    stretch taken out from under it, which would reset `stretch_seq`/`defer_count` and
    announce a second stretch for one block. The predicate is on `closed_at` because the
    table has no `created_at` worth pruning by - a stretch's life is its block, not its age.
    """
    from .outbox import _configured_retention

    days = retention_days if retention_days is not None else _configured_retention(conn)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM job_defer_stretch "
            "WHERE closed_at IS NOT NULL AND closed_at < NOW() - INTERVAL %s DAY",
            (days,),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted
