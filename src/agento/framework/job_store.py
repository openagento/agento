"""Job store — DB helpers for job lifecycle transitions."""
from __future__ import annotations

import os
import signal
import time

from .job_models import Job, JobStatus
from .toolbox_capability import revoke_job_capabilities


def fetch_job(conn, job_id: int, *, for_update: bool = False) -> Job | None:
    """Fetch a single job by ID. Returns None if not found.

    ``for_update`` takes a row lock, so a caller that decides on the status it
    reads is serialised against a concurrent writer of that same row.
    """
    sql = "SELECT * FROM job WHERE id = %s"
    if for_update:
        sql += " FOR UPDATE"
    with conn.cursor() as cur:
        cur.execute(sql, (job_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return Job.from_row(row)


def pause_job(conn, job_id: int) -> Job:
    """Transition a RUNNING job to PAUSED. Sends SIGTERM to the subprocess if alive.

    Flips the DB status FIRST (atomically), then signals the subprocess.
    This order closes the race where the agent finishes during the SIGTERM
    wait and the consumer's _finalize_job commits SUCCESS before pause's
    UPDATE fires.

    Returns the updated Job.
    Raises ValueError if job is not found or no longer in RUNNING status
    at the moment the UPDATE executes.
    """
    # FOR UPDATE: without the lock this read is stale the moment the consumer
    # mints the run's capabilities, and the revoke below would find nothing.
    job = fetch_job(conn, job_id, for_update=True)
    if job is None:
        raise ValueError(f"Job not found: id={job_id}")
    if job.status != JobStatus.RUNNING:
        raise ValueError(f"Cannot pause job in status {job.status.value}")

    # Step 1: atomically claim the pause. If the job raced to a terminal
    # state between our fetch and this UPDATE, rowcount will be 0.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job
            SET status = 'PAUSED', finished_at = NOW(), updated_at = NOW()
            WHERE id = %s AND status = 'RUNNING'
            """,
            (job_id,),
        )
        rows = cur.rowcount
    if rows:
        # Same transaction as the status flip: a paused job must not keep a live
        # capability, and a failing revoke must not leave the job PAUSED.
        revoke_job_capabilities(conn, job_id, commit=False)
    conn.commit()

    if rows == 0:
        # Re-read to report the actual status
        current = fetch_job(conn, job_id)
        actual = current.status.value if current else "UNKNOWN"
        raise ValueError(
            f"Cannot pause job {job_id}: status changed to {actual} "
            "before pause could be applied."
        )

    # Step 2: now that DB reflects PAUSED, signal the subprocess. If the
    # subprocess exits cleanly, consumer's _finalize_job sees PAUSED and
    # skips the SUCCESS/DEAD write.
    if job.pid is not None:
        try:
            os.kill(job.pid, 0)
            os.kill(job.pid, signal.SIGTERM)
            for _ in range(6):
                time.sleep(0.5)
                try:
                    os.kill(job.pid, 0)
                except OSError:
                    break
        except OSError:
            pass  # PID already dead — status is already PAUSED

    job.status = JobStatus.PAUSED
    return job


def resume_job(conn, job_id: int) -> Job:
    """Transition a PAUSED job back to TODO for re-pickup by the consumer.

    Clears the PID but preserves session_id and attempt so the consumer's
    existing auto-resume path fires on the next claim.

    Returns the updated Job.
    Raises ValueError if job is not found, not PAUSED, or missing session_id.
    """
    job = fetch_job(conn, job_id)
    if job is None:
        raise ValueError(f"Job not found: id={job_id}")
    if job.status != JobStatus.PAUSED:
        raise ValueError(f"Cannot resume job in status {job.status.value}")
    if job.session_id is None:
        raise ValueError(
            f"Cannot resume job {job_id}: no session_id. "
            "The agent session was not captured before pause."
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job
            SET status = 'TODO', pid = NULL, scheduled_after = NOW(), updated_at = NOW()
            WHERE id = %s AND status = 'PAUSED'
            """,
            (job_id,),
        )
    conn.commit()

    job.status = JobStatus.TODO
    job.pid = None
    return job
