from agento.framework.job_models import OPEN_STATUSES
from agento.framework.threads import default_ref, ensure_thread

BATCH_SIZE = 500


class AssignOpenJobThreads:
    """Give every open job from before threads (migration 054) its default thread.

    A job that is TODO, RUNNING or PAUSED at upgrade would otherwise reach the claim with no
    thread, and the one-open-turn-per-line rule (framework/threads.py) would not see it as
    an earlier turn of its thread. A finished job keeps `thread_id` NULL: nothing reads its
    thread. Read in batches of `BATCH_SIZE`, one commit per row (open jobs are few); a row
    that has a thread is never selected again, so a rerun is a no-op.
    """

    def apply(self, conn):
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source, reference_id, agent_view_id FROM job "
                    "WHERE thread_id IS NULL AND status IN %s ORDER BY id LIMIT %s",
                    (OPEN_STATUSES, BATCH_SIZE),
                )
                rows = cur.fetchall()
                if not rows:
                    conn.commit()
                    return
            conn.commit()
            # Row by row, each in its own transaction that locks the job before its thread:
            # the order a live claim takes them in (`place_claim`), so the two never deadlock.
            for row in rows:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM job WHERE id = %s AND thread_id IS NULL FOR UPDATE",
                                (row["id"],))
                    if cur.fetchone() is not None:      # else a claim gave it a thread first
                        thread_id = ensure_thread(
                            cur, agent_view_id=row["agent_view_id"],
                            ref=default_ref(row["source"], row["reference_id"], row["id"]))
                        cur.execute("UPDATE job SET thread_id = %s WHERE id = %s",
                                    (thread_id, row["id"]))
                conn.commit()

    def require(self):
        return []
