"""§5.3's finalizer — the module's writes, in the framework's transaction (PRD E3-E5 §6.4.2).

`execution` and `message` are module tables, and §6.4.1 requires the framework to keep
working with this module disabled, so the framework may not write them. It calls this
instead, on its own open connection, inside the transaction that ends the attempt: either
the transition and these rows are both there or neither is.

What it writes, per call:

* `execution.status` from `outcome` — **always**. A retried attempt closes its execution
  while its job goes back to `TODO`; a finalizer bound to the terminal status alone would
  leave those rows `running` for ever, which is the "turn in progress for ever" this exists
  to prevent.
* `message.job_state = 'terminal'` — **only** when `job_terminal`. That is §3.2's rule
  expressed as an argument rather than as a list of call sites.
* the assistant `message` row and its `assistant.message` event — only when the run
  actually produced an answer. A panel thread gets the event through the outbox, in order
  with its `job.*` events; a channel thread (E9 §3.5) has no `job.*` events and gets it
  directly.
* a `run.finished` event in the run's thread, for every outcome.

It deliberately does **not** write `job.failed`: that is a framework job transition, and a
module that owned it would quietly drop it from the module-disabled guarantee.
"""
from __future__ import annotations

import logging

from agento.framework.outbox import write_outbox

from .workflow import ReferenceUnusable, parse_reference

logger = logging.getLogger(__name__)

TRUNCATION_MARKER = "\n\n[…]"


def truncate_utf8(text: str, max_bytes: int, marker: str = TRUNCATION_MARKER) -> str:
    """`text` bounded to `max_bytes` UTF-8 bytes, cut on a codepoint boundary.

    The run already happened, so an over-long answer is truncated and never rejected
    (§10.2). The cut is on a character, not a byte: slicing the encoded form at an arbitrary
    offset can land mid-codepoint and produce bytes that are not UTF-8 at all — which the
    column, the JSON payload and the API response would each reject or mangle in turn.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    budget = max_bytes - len(marker.encode("utf-8"))
    if budget <= 0:
        # A marker that does not fit is not a reason to emit an unbounded row; the bound
        # wins and the caller gets as much of the marker as the limit allows.
        return marker.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    # `errors="ignore"` on the DECODE is what makes the cut land on a codepoint boundary:
    # a partial trailing sequence is dropped rather than replaced.
    return text.encode("utf-8")[:budget].decode("utf-8", "ignore") + marker


class ConversationFinalizer:
    """Registered as the module's `execution_finalizer` in `di.json`."""

    def finalize(self, *, conn, job_id: int, attempt: int, execution_id: str | None,
                 outcome: str, job_terminal: bool) -> None:
        from . import service

        with conn.cursor() as cur:
            resolved = self._close_execution(cur, job_id, attempt, execution_id, outcome)
            run = self._run(cur, resolved)
            if run is not None:
                # Takes the thread's row lock first: the lock-order invariant (E9 §3.4).
                service.append_event(
                    cur, run["conversation_id"], kind="run.finished", execution_id=resolved,
                    source_kind="execution_end", source_id=run["id"],
                    payload={"job_id": job_id, "attempt": attempt, "outcome": outcome})
            reference = self._conversation_reference(cur, job_id)
            if reference is None:
                if run is not None and outcome == "succeeded":
                    self._write_answer(cur, run["conversation_id"], job_id, resolved,
                                       channel=True)
                return
            conversation_id, message_id = reference
            service.lock_conversations(cur, [conversation_id])
            if job_terminal:
                cur.execute(
                    "UPDATE message SET job_state = 'terminal' WHERE id = %s", (message_id,))
            if outcome == "succeeded" and resolved is not None:
                self._write_answer(cur, conversation_id, job_id, resolved)

    # --- the execution row ------------------------------------------------

    def _close_execution(self, cur, job_id, attempt, execution_id, outcome) -> str | None:
        """Move this attempt out of `running`. Returns the execution id it settled on.

        `execution_id` is None on the recovery paths: they see another process's run and
        never held its id, so they are resolved by `(job_id, attempt)` instead. That key is
        deliberately not unique (the pool wait refunds an attempt), so the newest row wins -
        it is the one the recovered process was running.
        """
        if execution_id is None:
            cur.execute(
                "SELECT execution_id FROM execution WHERE job_id = %s AND attempt = %s "
                "AND status = 'running' ORDER BY id DESC LIMIT 1", (job_id, attempt))
            row = cur.fetchone()
            if row is None:
                return None
            execution_id = row["execution_id"]
        cur.execute(
            "UPDATE execution SET status = %s, finished_at = NOW() "
            "WHERE execution_id = %s AND status = 'running'", (outcome, execution_id))
        return execution_id

    # --- the thread -------------------------------------------------------

    def _run(self, cur, execution_id: str | None) -> dict | None:
        """The execution row with its thread, or None when it has none."""
        if execution_id is None:
            return None
        cur.execute("SELECT id, conversation_id FROM execution WHERE execution_id = %s",
                    (execution_id,))
        row = cur.fetchone()
        return row if row is not None and row["conversation_id"] is not None else None

    def _conversation_reference(self, cur, job_id: int) -> tuple[int, int] | None:
        """`(conversation_id, message_id)` for a conversation job, else None.

        By `job.source` first, exactly as the relay: another module's job may carry a
        `reference_id` that reads like a conversation's.
        """
        cur.execute("SELECT source, reference_id FROM job WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if job is None or job["source"] != "conversation":
            return None
        try:
            _, message_id = parse_reference(job["reference_id"])
        except ReferenceUnusable:
            logger.warning("job %s has an unusable reference_id %r; nothing finalized",
                           job_id, job["reference_id"])
            return None
        cur.execute("SELECT conversation_id FROM message WHERE id = %s", (message_id,))
        message = cur.fetchone()
        if message is None:
            logger.warning("job %s references message %s, which does not exist", job_id,
                           message_id)
            return None
        return message["conversation_id"], message_id

    # --- the answer -------------------------------------------------------

    def _write_answer(self, cur, conversation_id: int, job_id: int,
                      execution_id: str, *, channel: bool = False) -> None:
        """The assistant row and its event, or nothing.

        The two writes have DIFFERENT uniqueness: the message row is pinned by
        `(conversation_id, execution_id)`, while every outbox row is a fresh id. So the
        event is written **only when the insert actually won**. Written unconditionally
        beside it, a replayed terminal transaction would re-use the one message row and
        still emit a second `assistant.message` - and the relay, faithful by design, would
        deliver the same answer to the thread twice. An idempotent write beside a
        non-idempotent one is not an idempotent transaction.
        """
        cur.execute("SELECT output FROM job WHERE id = %s", (job_id,))
        row = cur.fetchone()
        answer = (row or {}).get("output")
        if not answer:
            return

        from . import service

        answer = truncate_utf8(answer, service.config(cur.connection,
                                                      "limits/max_message_bytes"))
        cur.execute(
            # The assistant row carries NO `job_id` and NO `job_state`: the reply is not
            # itself a queued turn, and §4.4's non-terminal check reads `user` rows only.
            "INSERT IGNORE INTO message (conversation_id, role, content, execution_id) "
            "VALUES (%s, 'assistant', %s, %s)",
            (conversation_id, answer, execution_id),
        )
        if cur.rowcount != 1:
            return
        message_id = cur.lastrowid
        payload = {"message_id": message_id, "content": answer}
        if channel:
            service.append_event(cur, conversation_id, kind="assistant.message",
                                 execution_id=execution_id, source_kind="answer",
                                 source_id=message_id, payload=payload)
            return
        write_outbox(cur, job_id=job_id, execution_id=execution_id,
                     kind="assistant.message", payload=payload)
