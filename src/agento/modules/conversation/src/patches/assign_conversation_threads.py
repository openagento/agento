from agento.framework.job_models import OPEN_STATUSES
from agento.framework.threads import default_ref, ensure_thread

from ..service import PANEL_CHANNEL, panel_thread_ref

BATCH_SIZE = 500


def _ref(cur, row: dict) -> str:
    """The thread a conversation from before threads continues in.

    A panel conversation: its own thread, the one `create_conversation` gives a new one. A
    channel conversation was keyed by `(source, view, reference)` of its jobs, so its
    thread is the one a new job of that channel computes (`default_ref`) and its history
    continues into new jobs. One with no reference held one job (`job:<id>`): the job of
    its latest run.
    """
    if row["channel"] == PANEL_CHANNEL:
        return panel_thread_ref(row["id"])
    if row["external_ref"]:
        return default_ref(row["channel"], row["external_ref"], 0)
    cur.execute("SELECT job_id FROM execution WHERE conversation_id = %s ORDER BY id DESC "
                "LIMIT 1", (row["id"],))
    run = cur.fetchone()
    return default_ref(row["channel"], None, run["job_id"]) if run else _legacy_ref(row)


def _legacy_ref(row: dict) -> str:
    return f"legacy:{row['external_key'] or row['id']}"


class AssignConversationThreads:
    """Give every conversation from before migration 007 its thread (1:1), then move the
    open jobs of each conversation into it.

    The core patch `AssignOpenJobThreads` gives an open job its default thread. For a
    queued panel turn that is a thread of its own message, and `link_execution` would show
    its run nowhere; for a queued channel reply it is a thread of its own item, and
    `link_execution` would open a second, ownerless conversation for it. The last two UPDATEs put every open job
    of a conversation - through its turn's `message` and through a running `execution` -
    into the conversation's thread, whichever of the two patches ran first. A thread that
    another conversation already holds is not shared: the second one takes its own
    `legacy:` thread. A rerun selects nothing and repeats the idempotent UPDATEs. It
    assumes the consumer is stopped, as `setup:upgrade` runs before it starts: the closing
    UPDATEs lock many job rows in one statement, and a claim between migration 007 and this
    patch would open a second conversation for its thread.
    """

    def apply(self, conn):
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM conversation WHERE thread_id IS NULL ORDER BY id LIMIT %s",
                    (BATCH_SIZE,))
                ids = [r["id"] for r in cur.fetchall()]
            conn.commit()
            if not ids:
                break
            for conversation_id in ids:
                self._assign(conn, conversation_id)
        with conn.cursor() as cur:
            for via in ("message m ON m.job_id = j.id JOIN conversation c "
                        "ON c.id = m.conversation_id",
                        "execution e ON e.job_id = j.id JOIN conversation c "
                        "ON c.id = e.conversation_id"):
                cur.execute(f"UPDATE job j JOIN {via} SET j.thread_id = c.thread_id "
                            "WHERE j.status IN %s AND c.thread_id IS NOT NULL",
                            (OPEN_STATUSES,))
        conn.commit()

    def _assign(self, conn, conversation_id: int) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT id, agent_view_id, channel, external_ref, external_key "
                        "FROM conversation WHERE id = %s AND thread_id IS NULL FOR UPDATE",
                        (conversation_id,))
            row = cur.fetchone()
            if row is not None:
                view = row["agent_view_id"]
                thread_id = ensure_thread(cur, agent_view_id=view, ref=_ref(cur, row))
                cur.execute("SELECT 1 FROM conversation WHERE thread_id = %s", (thread_id,))
                if cur.fetchone() is not None:
                    thread_id = ensure_thread(cur, agent_view_id=view, ref=_legacy_ref(row))
                cur.execute("UPDATE conversation SET thread_id = %s WHERE id = %s",
                            (thread_id, conversation_id))
        conn.commit()

    def require(self):
        return []
