"""The outbox relay (PRD E3-E5 §6.4.1).

The framework writes a job transition into `job_event_outbox` in the transaction that made
it, knowing nothing about who reads it. This is the one reader: it turns the rows that
belong to a conversation into `conversation_event` rows and marks every row it looked at
relayed, so nothing stays pending.

**The classification order is the contract, and inverting it loses events.** A row is
classified by `job.source` FIRST, never by its reference value:

1. no `job` row (jobs are pruned on their own schedule) → terminally relayed, `WARN`, no
   event. Without `source` nothing can classify it, and `message.job_id` cannot stand in:
   §4.1 sets that column in a second commit, so its absence proves nothing.
2. `job.source != 'conversation'` → relayed, no event, whatever its `reference_id` — an
   unrelated job may carry a reference equal to some `message.id` by coincidence.
3. `source = 'conversation'` with a resolvable `reference_id` → one event.
4. `source = 'conversation'`, unresolvable → terminally relayed with a `WARN`. That is a
   data fault, not a race: §4.3 writes `reference_id` in the job's own insert.

The resolution reads `message.id` (from `reference_id`), **never** `message.job_id`. §4.1
inserts the job in one transaction and sets `message.job_id` in the next, so a relay tick
between the two would find no message and mark `job.queued` relayed with no event — losing
the first event of every thread.

One relay process, claiming under a row lock, in `id` order: `conversation_event.id` is
assigned at relay time in outbox order, which is what makes a thread's lifecycle totally
ordered when producers interleave. An inline post-commit relay beside this one would be a
second writer and could invert that order, so there deliberately is none.
"""
from __future__ import annotations

import json
import logging

from . import service
from .workflow import ReferenceUnusable, parse_reference

logger = logging.getLogger(__name__)

SOURCE_KIND = "outbox"


def relay_outbox(conn, *, limit: int = 500) -> int:
    """Relay one batch of unrelayed outbox rows. Returns how many rows were consumed.

    Every row in the batch commits together: the events and the `relayed_at` marks are one
    transaction, so a crash re-runs the whole batch and `UNIQUE (source_kind, source_id)`
    absorbs the repeat.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.id, o.job_id, o.execution_id, o.kind, o.payload, "
            "       j.source, j.reference_id "
            "FROM job_event_outbox o LEFT JOIN job j ON j.id = o.job_id "
            "WHERE o.relayed_at IS NULL ORDER BY o.id LIMIT %s "
            # Only the outbox rows are locked: a job row is not ours to hold, and locking
            # one here would block the consumer that is still writing transitions.
            "FOR UPDATE OF o",
            (limit,),
        )
        rows = list(cur.fetchall())
        owners = _owning_conversations(cur, rows)
        targets = [(row, _classify(row, owners)) for row in rows]
        service.lock_conversations(cur, [c for _, c in targets if c is not None])
        for row, conversation_id in targets:
            if conversation_id is not None:
                _write_event(cur, row, conversation_id)
        if rows:
            cur.execute(
                f"UPDATE job_event_outbox SET relayed_at = NOW() "
                f"WHERE id IN ({','.join(['%s'] * len(rows))})",
                [row["id"] for row in rows],
            )
    conn.commit()
    return len(rows)


def _referenced_message(row) -> int | None:
    """The message id this row points at, or None when it points at nothing usable."""
    try:
        return parse_reference(row["reference_id"])[1]
    except ReferenceUnusable:
        return None


def _owning_conversations(cur, rows) -> dict[int, int]:
    """{message_id: conversation_id} for the whole batch, in ONE query.

    The message's OWN conversation_id, not the one in the reference: the reference is a
    convenience for the workflow, the message row is the fact. Resolved per batch rather
    than per row - a 500-row batch used to mean 500 selects for the same work (CODE-8).
    """
    wanted = {
        message_id
        for row in rows
        if row["source"] == "conversation"
        and (message_id := _referenced_message(row)) is not None
    }
    if not wanted:
        return {}
    cur.execute(
        f"SELECT id, conversation_id FROM message WHERE id IN ({','.join(['%s'] * len(wanted))})",
        list(wanted))
    return {row["id"]: row["conversation_id"] for row in cur.fetchall()}


def _classify(row, owners: dict[int, int]) -> int | None:
    """The conversation this row belongs to, or None when it yields no event."""
    if row["source"] is None:
        logger.warning(
            "outbox row %s: job %s is gone, relayed with no event (kind=%s)",
            row["id"], row["job_id"], row["kind"],
        )
        return None
    if row["source"] != "conversation":
        return None

    message_id = _referenced_message(row)
    if message_id is None:
        logger.warning(
            "outbox row %s: job %s has an unusable reference_id %r, relayed with no event",
            row["id"], row["job_id"], row["reference_id"],
        )
        return None

    conversation_id = owners.get(message_id)
    if conversation_id is None:
        logger.warning(
            "outbox row %s: job %s references message %s, which does not exist; "
            "relayed with no event", row["id"], row["job_id"], message_id,
        )
        return None
    return conversation_id


def _write_event(cur, row, conversation_id: int) -> None:
    payload = row["payload"]
    service.append_event(
        cur, conversation_id, kind=row["kind"], execution_id=row["execution_id"],
        payload=json.loads(payload) if isinstance(payload, str) else payload,
        source_kind=SOURCE_KIND, source_id=row["id"], locked=True)


def prune_relayed(conn, *, retention_days: int) -> int:
    """Delete relayed rows past the module's own window.

    Unrelayed rows are left alone here: the framework's `core/outbox/retention_days` is the
    backstop that bounds them even with this module disabled (CODE-8), and deleting one
    from under a relay that has not read it yet would lose the event.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM job_event_outbox WHERE relayed_at IS NOT NULL "
            "AND created_at < NOW() - INTERVAL %s DAY",
            (retention_days,),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted


TOOL_SOURCE_KIND = "tool_invocation"
TOOL_EVENT_KIND = "tool.called"

# The tool audit is written by the toolbox, in Node, with no idea a conversation exists.
# This projection is the whole of the conversation's knowledge of it: no Node code writes a
# conversation event, and none may.
_TOOL_BATCH_SQL = (
    "SELECT t.id, t.run_execution_id, t.tool_name, t.outcome, "
    "       t.agent_view_id, t.workspace_id, e.conversation_id "
    "FROM tool_invocation t "
    # `execution` is the only bridge from a run's execution id to its thread (E9 §3.5).
    # Joining through `toolbox_capability` instead would lose every call whose capability
    # has been purged, and the audit deliberately outlives its capability.
    "JOIN execution e ON e.execution_id = t.run_execution_id "
    "WHERE t.conversation_relayed_at IS NULL AND t.run_execution_id IS NOT NULL "
    # A call still running has no outcome yet. Leaving it unclaimed is not "pending
    # forever": the dispatcher finalizes every exit, so the next tick projects it with the
    # outcome it really had. Projecting `pending` would freeze that guess into an
    # append-only row.
    "AND t.outcome <> 'pending' "
    "ORDER BY t.id LIMIT %s FOR UPDATE OF t"
)


def project_tool_calls(conn, *, limit: int = 500) -> int:
    """Project finished tool calls into `tool.called` events. Returns rows consumed.

    The checkpoint is `tool_invocation.conversation_relayed_at`, on the producer row - not
    the absence of an event. §10.1 prunes events, and a checkpoint read off the event table
    would project a pruned `tool.called` again on the next tick.
    """
    with conn.cursor() as cur:
        cur.execute(_TOOL_BATCH_SQL, (limit,))
        rows = list(cur.fetchall())
        # A run with no thread (minted before runs were linked) yields no event.
        service.lock_conversations(cur, [r["conversation_id"] for r in rows
                                         if r["conversation_id"] is not None])
        for row in rows:
            if row["conversation_id"] is not None:
                _write_tool_event(cur, row, row["conversation_id"])
        if rows:
            cur.execute(
                f"UPDATE tool_invocation SET conversation_relayed_at = NOW() "
                f"WHERE id IN ({','.join(['%s'] * len(rows))})",
                [row["id"] for row in rows],
            )
    conn.commit()
    return len(rows)


def _write_tool_event(cur, row, conversation_id: int) -> None:
    service.append_event(
        cur, conversation_id, kind=TOOL_EVENT_KIND, execution_id=row["run_execution_id"],
        source_kind=TOOL_SOURCE_KIND, source_id=row["id"], locked=True,
        # The name, the scope and the outcome - never an argument or a result. The audit
        # itself only ever held a digest of the arguments, and this is one hop closer to a
        # browser than the audit is.
        payload={
            "tool_name": row["tool_name"],
            "outcome": row["outcome"],
            "tool_invocation_id": row["id"],
            "execution_id": row["run_execution_id"],
            "agent_view_id": row["agent_view_id"],
            "workspace_id": row["workspace_id"],
        })
