"""Retention, auto-archive and the expired cursor (PRD E3-E5 §10.1).

Three passes and one question.

**The event prune is by age, per conversation, and it is what moves the watermark.**
`retention/event_days` deletes `conversation_event` rows by `created_at` in **live**
conversations, not only in ones being deleted. The predicate is strict `<`, so a row exactly
on the cutoff is kept. The watermark is set to the **highest id just removed** - not the
highest surviving one - in the same transaction, and it is **monotonic**: only ever raised, so
a later pass that removes nothing leaves it alone and a second pass over the same rows changes
nothing.

**The delete is an explicit ordered delete, not a cascade.** One transaction per conversation:
lock the `conversation` row `FOR UPDATE` and re-check under that lock that it is still
`archived` and still past the window - the posting path takes the same lock, so reactivation
and deletion serialize and a post either wins (the thread survives) or loses (the delete
completes). Never half of each. A crash mid-delete leaves a still-archived conversation the
next run finishes, never an orphan.

**`cursor_expired` is per conversation and is asked on BOTH cursor paths.** A cursor at or
below that conversation's watermark is expired; above it with no rows is up to date; a
conversation with no watermark row can have no expired cursor. Checking only the replay route
is the worse of the two failures: a browser reconnecting after a prune would be handed the
surviving rows with the removed ones silently missing and no signal that anything is gone - a
gap presented as complete history.
"""
from __future__ import annotations

import logging

from . import service

logger = logging.getLogger(__name__)

MIN_EVENT_DAYS = 1

# What ONE run may take on. Every pass loads its ids before it works on them, so an
# unbounded SELECT is an unbounded list in memory, and one nightly run that never ends
# (CODE-8). The cron runs daily; what a run leaves is what the next run starts with, and
# the passes are ordered so nothing is skipped for ever - the oldest is always taken first.
MAX_PER_RUN = 500

# And what one run may DELETE. Capping the conversations alone capped nothing: one long
# thread's whole backlog still went in a single transaction (CODE-8).
MAX_EVENT_ROWS_PER_RUN = 10_000


def cursor_expired(conn, *, conversation_id: int, cursor: int | None) -> bool:
    """The one question both cursor paths ask. `None` (no cursor) can never be expired."""
    if cursor is None:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT last_pruned_event_id FROM conversation_prune_watermark "
                    "WHERE conversation_id = %s", (conversation_id,))
        row = cur.fetchone()
    return row is not None and cursor <= int(row["last_pruned_event_id"])


# --- the age prune ---------------------------------------------------------

def prune_events(conn, *, event_days: int, limit: int = MAX_PER_RUN,
                 max_rows: int = MAX_EVENT_ROWS_PER_RUN) -> int:
    """Delete events past the window, per conversation, and raise each watermark.

    At most `limit` conversations per run: the ones with the oldest surviving event first,
    so a backlog drains in order instead of one run trying to hold every id at once.
    """
    days = max(MIN_EVENT_DAYS, event_days)
    removed = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conversation_id FROM conversation_event "
            "WHERE created_at < (NOW() - INTERVAL %s DAY) "
            "GROUP BY conversation_id ORDER BY MIN(id) LIMIT %s", (days, limit))
        ids = [row["conversation_id"] for row in cur.fetchall()]
    for conversation_id in ids:
        if removed >= max_rows:
            break                 # the rest is the next run's work, oldest first
        removed += _prune_one(conn, conversation_id, days, budget=max_rows - removed)
    return removed


def _prune_one(conn, conversation_id: int, days: int, *, budget: int) -> int:
    with conn.cursor() as cur:
        # The highest id being removed, read in the same transaction as the delete: a row
        # written between the two would otherwise be counted as pruned and never sent.
        # `budget` bounds it from below - the OLDEST rows up to the budget - so one long
        # thread cannot put its whole backlog into a single transaction, and the watermark
        # still names exactly what went (it only ever rises).
        cur.execute(
            "SELECT MAX(id) AS highest FROM "
            "  (SELECT id FROM conversation_event "
            "   WHERE conversation_id = %s AND created_at < (NOW() - INTERVAL %s DAY) "
            "   ORDER BY id LIMIT %s) AS oldest",
            (conversation_id, days, budget))
        highest = (cur.fetchone() or {}).get("highest")
        if highest is None:
            conn.commit()
            return 0
        cur.execute(
            "DELETE FROM conversation_event WHERE conversation_id = %s AND id <= %s "
            "AND created_at < (NOW() - INTERVAL %s DAY)",
            (conversation_id, highest, days))
        count = cur.rowcount
        cur.execute(
            # GREATEST, not a plain assignment: the watermark only ever rises, so a pass
            # that removes older rows after a later one cannot drag it backwards.
            "INSERT INTO conversation_prune_watermark "
            "(conversation_id, last_pruned_event_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "last_pruned_event_id = GREATEST(last_pruned_event_id, VALUES(last_pruned_event_id)), "
            "pruned_at = NOW()",
            (conversation_id, highest))
    conn.commit()
    return count


# --- auto-archive ----------------------------------------------------------

# Idle is the NEWEST MESSAGE's clock (§10.1: "a conversation whose last message is older
# than idle_days"), and nothing else. `conversation.updated_at` is not a second opinion to
# add to it: it moves when the row is written - an archive, a reactivation, a rename - so
# requiring it to be old too means one rename keeps a dead thread out of retention for
# ever. A thread with no messages at all has no message clock, and falls back to its own
# `created_at`, which is the only activity it has ever had. A non-terminal user turn is
# never idle whatever either clock says: it is waiting for an answer.
#
# A CHANNEL thread (`user_id IS NULL`, E9 §3.5) has no user turns: its clock is the newest
# event (`last_activity_at`), so a later run's events keep it alive past an old answer, and
# it is never idle while one of its runs is still running.
_IDLE = (
    "c.status = 'active' "
    "AND ((c.user_id IS NOT NULL "
    "  AND COALESCE("
    "        (SELECT MAX(m.created_at) FROM message m WHERE m.conversation_id = c.id), "
    "        c.created_at"
    "      ) < (NOW() - INTERVAL %s DAY) "
    "  AND NOT EXISTS ("
    "        SELECT 1 FROM message m WHERE m.conversation_id = c.id "
    "          AND m.role = 'user' AND m.job_state <> 'terminal')) "
    "OR (c.user_id IS NULL "
    "  AND COALESCE(c.last_activity_at, c.created_at) < (NOW() - INTERVAL %s DAY) "
    "  AND NOT EXISTS ("
    "        SELECT 1 FROM execution e WHERE e.conversation_id = c.id "
    "          AND e.status = 'running')))"
)


def still_idle(conn, conversation_id: int, *, idle_days: int) -> bool:
    """Is this thread STILL idle, under the row lock the posting path takes?

    The lock is held until the caller commits - `service.archive`'s commit - so a post
    that arrives after the bulk select either landed before this read (and the answer is
    False) or waits for the archive and reactivates a thread that is already archived.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT c.id FROM conversation c WHERE c.id = %s AND {_IDLE} "
                    "FOR UPDATE", (conversation_id, idle_days, idle_days))
        return cur.fetchone() is not None


def auto_archive(conn, *, idle_days: int, limit: int = MAX_PER_RUN) -> int:
    """Retire an idle thread through `service.archive` - the ONE archive path (§10.1).

    A thread whose newest **user** turn is not terminal is skipped, paused ones included: it
    is waiting for an answer, not idle, and archiving it would hide a thread the user is
    still owed a reply on.

    Selected in one query, then re-checked per thread under the row lock the posting path
    takes (`service.reactivate`): between the select and the archive a user can post, and
    archiving then would retire a thread that is live again. The lock is held from that
    re-check until `service.archive` commits, so the two serialize.
    """
    with conn.cursor() as cur:
        # Oldest first, `limit` per run - see MAX_PER_RUN.
        cur.execute(f"SELECT c.id FROM conversation c WHERE {_IDLE} ORDER BY c.id LIMIT %s",
                    (idle_days, idle_days, limit))
        ids = [row["id"] for row in cur.fetchall()]
    conn.commit()
    archived = 0
    for conversation_id in ids:
        if not still_idle(conn, conversation_id, idle_days=idle_days):
            conn.commit()           # a post landed while we queued for the lock; it wins
            continue
        archived += service.archive(conn, conversation_id, actor_id=None, reason="idle")
    return archived


# --- old runs -----------------------------------------------------------------

def prune_old_executions(conn, *, event_days: int, limit: int = 100,
                         max_rows: int = MAX_PER_RUN) -> int:
    """Delete finished `execution` rows (and their deltas) past the event window.

    One age bound for every run, linked to a thread or not (E9 §3.5): an active channel
    thread lives for as long as its issue keeps running, and its runs must not grow with
    it for ever (CODE-8). The clock is `finished_at` - the run's last event - because the
    event prune ages by `created_at`: a run that started long ago and finished today keeps
    its row while its events live. A running run is never pruned, nor one with a finished
    tool call the relay has not projected yet (the relay needs the row to find the thread;
    the predicate is `relay._TOOL_BATCH_SQL`'s). Oldest first, `limit` per pass, so one
    sweep cannot lock the table behind a backlog.
    """
    days = max(MIN_EVENT_DAYS, event_days)
    removed = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT execution_id FROM execution e "
                "WHERE e.status <> 'running' "
                "AND e.finished_at < (NOW() - INTERVAL %s DAY) "
                "AND NOT EXISTS (SELECT 1 FROM tool_invocation t "
                "  WHERE t.run_execution_id = e.execution_id "
                "    AND t.conversation_relayed_at IS NULL AND t.outcome <> 'pending') "
                "ORDER BY e.id LIMIT %s", (days, min(limit, max_rows - removed)))
            ids = [row["execution_id"] for row in cur.fetchall()]
            if not ids:
                conn.commit()
                return removed
            marks = ", ".join(["%s"] * len(ids))
            cur.execute(f"DELETE FROM execution_delta WHERE execution_id IN ({marks})", ids)
            cur.execute(f"DELETE FROM execution WHERE execution_id IN ({marks})", ids)
            removed += cur.rowcount
        conn.commit()
        if len(ids) < limit or removed >= max_rows:
            return removed


# --- the delete ------------------------------------------------------------

def delete_archived(conn, *, archived_days: int, limit: int = MAX_PER_RUN) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM conversation WHERE status = 'archived' "
            "AND updated_at < (NOW() - INTERVAL %s DAY) ORDER BY updated_at LIMIT %s",
            (archived_days, limit))
        ids = [row["id"] for row in cur.fetchall()]
    conn.commit()
    return sum(1 for cid in ids if delete_tree(conn, cid, require_archived_days=archived_days))


def delete_tree(conn, conversation_id: int, *, require_archived_days: int | None = None) -> bool:
    """One conversation, one transaction, in dependency order and with no cascade relied on.

    `require_archived_days` is the retention pass's re-check **under the lock**; the operator
    path passes None and deletes what it was told to.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, status, updated_at FROM conversation WHERE id = %s "
                    "FOR UPDATE", (conversation_id,))
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return False
        if require_archived_days is not None:
            cur.execute(
                "SELECT 1 AS ok FROM conversation WHERE id = %s AND status = 'archived' "
                "AND updated_at < (NOW() - INTERVAL %s DAY)",
                (conversation_id, require_archived_days))
            if cur.fetchone() is None:
                # A post reactivated it while we queued for the lock. It wins; the thread
                # survives whole, and this pass simply skips it.
                conn.commit()
                return False

        cur.execute("DELETE FROM conversation_event WHERE conversation_id = %s",
                    (conversation_id,))
        cur.execute(
            "DELETE d FROM execution_delta d "
            "JOIN execution e ON e.execution_id = d.execution_id "
            "WHERE e.conversation_id = %s", (conversation_id,))
        cur.execute("DELETE FROM execution WHERE conversation_id = %s", (conversation_id,))
        cur.execute("DELETE FROM message WHERE conversation_id = %s", (conversation_id,))
        cur.execute("DELETE FROM conversation_prune_watermark WHERE conversation_id = %s",
                    (conversation_id,))
        cur.execute("DELETE FROM conversation WHERE id = %s", (conversation_id,))
    conn.commit()
    return True
