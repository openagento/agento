"""The delta sink: the only writer of `execution_delta` and of `assistant.delta` (§6.4.1, §8.2).

The framework owns the queue and the writer thread; this file owns the rows. Everything here
runs on that thread, off the harness's drain path, on its **own** connection — the seam is
deliberately the one of the four that gets no connection, because it must not hold the run's
transaction open while a subprocess is still producing output.

Two rows per fragment, in one transaction:

* `execution_delta` — the ordering and cap ledger, unique on `(execution_id, seq)`, which is
  what makes a re-delivered fragment harmless.
* `conversation_event` — what a reader actually sees, keyed `('delta', <the ledger row id>)`
  so the same uniqueness carries into the stream.

**The caps are the module's, not the framework's** (§8.2). Exceeding either
`stream/max_deltas_per_execution` or `stream/max_delta_bytes_per_execution` stops the deltas
for that execution and records **one** `truncated` marker. The final assistant message is
written by the finalizer and is never truncated: a cap on the live stream is not a cap on the
answer.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.execution_hooks import DeltaRecord

from . import service

logger = logging.getLogger(__name__)


def _sql_timeout_seconds(conn) -> int:
    """`core/sql_timeout_seconds`, through the framework's own 3-level fallback."""
    from pathlib import Path

    from agento.framework.config_resolver import (
        load_db_overrides,
        read_config_defaults,
        resolve_field,
    )

    core = Path(service.MODULE_DIR).parent / "core"
    schema = json.loads((core / "system.json").read_text())["sql_timeout_seconds"]
    return int(resolve_field("core", "sql_timeout_seconds", schema,
                             read_config_defaults(core), load_db_overrides(conn)).value)

SOURCE_KIND = "delta"
EVENT_KIND = "assistant.delta"


# How many executions one sink keeps accounted in memory. Well above the executions a
# consumer streams at once (`AGENTO_CONSUMER_MAX_WORKERS` defaults to 10), so eviction is
# a ceiling and not something a normal run meets.
_MAX_TRACKED_EXECUTIONS = 512


class _Budget:
    """What one execution has spent. Seeded from the database the first time it is seen, so
    a restart resumes the count instead of handing the execution a fresh allowance."""

    __slots__ = ("bytes", "conversation", "count", "truncated")

    def __init__(self, count: int, size: int, truncated: bool) -> None:
        self.count, self.bytes, self.truncated = count, size, truncated
        # The conversation this execution belongs to, remembered the first time it
        # RESOLVES: it cannot change afterwards, and re-reading it per fragment is a
        # three-table join per streamed token (CODE-8). A miss is deliberately not cached:
        # §4.1 sets `message.job_id` in a second commit, so the first fragments of a run
        # can arrive before the join can see the thread, and freezing that miss would
        # silently drop every event of exactly that run.
        self.conversation: int | None = None


class ConversationDeltaSink:
    """`ExecutionDeltaSink`. One instance per bootstrap; the thread adopts the newest."""

    def __init__(self) -> None:
        self._conn = None
        self._budgets: dict[str, _Budget] = {}

    # -- the seam ------------------------------------------------------------

    def write(self, batch: Sequence[DeltaRecord]) -> None:
        conn = self._connection()
        # Both caps resolved ONCE per batch, not per fragment: `service.config` re-reads
        # every DB override and the module's system.json each time, and a batch is a burst
        # of streamed tokens (CODE-8). A batch is short, so a cap changed mid-stream takes
        # effect on the next one.
        caps = (service.config(conn, "stream/max_deltas_per_execution"),
                service.config(conn, "stream/max_delta_bytes_per_execution"))
        with conn.cursor() as cur:
            for record in batch:
                self._one(cur, record, caps)
        conn.commit()

    # -- the connection ------------------------------------------------------

    def _connection(self):
        """Kept open across batches, with a statement timeout.

        The timeout is what bounds the framework's handover wait: the writer thread holds
        the handover lock across this whole call, so a `write` that could block for ever
        would make disabling the module block for ever too.
        """
        if self._conn is not None and self._conn.open:
            return self._conn
        self._conn = get_connection(DatabaseConfig.from_env())
        seconds = max(1, _sql_timeout_seconds(self._conn))
        with self._conn.cursor() as cur:
            cur.execute("SET SESSION max_execution_time = %s", (seconds * 1000,))
            # MySQL applies `max_execution_time` to read-only SELECTs ONLY, and every write
            # here is an INSERT. Without this second knob the declared timeout bounded the
            # `_budget` reads and nothing else, so a row lock held elsewhere could park the
            # writer thread - and with it the handover lock, and with it disabling the
            # module - for `innodb_lock_wait_timeout`'s 50-second default or longer.
            cur.execute("SET SESSION innodb_lock_wait_timeout = %s", (seconds,))
        self._conn.commit()
        return self._conn

    # -- one fragment --------------------------------------------------------

    def _one(self, cur, record: DeltaRecord, caps: tuple[int, int]) -> None:
        budget = self._budget(cur, record.execution_id)
        if budget.truncated:
            # Already capped. A second marker would say nothing the first does not.
            return
        if record.kind == "delta" and self._over_cap(budget, record, caps):
            budget.truncated = True
            marker, _ = self._ledger(cur, record.execution_id, record.seq, "truncated")
            if marker is not None:
                self._event(cur, budget, record.execution_id, record.seq, "truncated",
                            None, None, ledger_id=marker)
            return
        row_id, fresh = self._ledger(cur, record.execution_id, record.seq, record.kind)
        if row_id is None:
            return
        if fresh:
            # Only a NEW fragment spends the budget; a re-delivery already paid.
            budget.count += 1
            budget.bytes += len((record.text or "").encode("utf-8"))
        # Attempted on a re-delivery too: the first attempt may have found no conversation
        # yet (§4.1 sets `message.job_id` in a second commit), and the ledger row alone
        # would then have swallowed the event for ever. `_event`'s own INSERT IGNORE on
        # `uq_source` is what makes the retry cost nothing when the event already exists.
        self._event(cur, budget, record.execution_id, record.seq, record.kind,
                    record.text, record.tool_name, ledger_id=row_id)

    def _over_cap(self, budget: _Budget, record: DeltaRecord, caps: tuple[int, int]) -> bool:
        max_deltas, max_bytes = caps
        size = len((record.text or "").encode("utf-8"))
        return budget.count + 1 > max_deltas or budget.bytes + size > max_bytes

    def _budget(self, cur, execution_id: str) -> _Budget:
        budget = self._budgets.get(execution_id)
        if budget is not None:
            # Newest-used last, so the eviction below drops the least recently written
            # execution and not whichever one happened to start first.
            self._budgets[execution_id] = self._budgets.pop(execution_id)
            return budget
        cur.execute(
            "SELECT COUNT(*) AS n, "
            "       SUM(kind = 'truncated') AS capped "
            "FROM execution_delta WHERE execution_id = %s", (execution_id,))
        row = cur.fetchone() or {}
        cur.execute(
            # `LENGTH(payload->>'$.text')` - the UTF-8 BYTES of the same text `_one`
            # counts, not the characters of the whole JSON row. On CHAR_LENGTH(payload)
            # the cap meant one thing while the process lived and another after a
            # restart: the envelope's keys were counted, every escape was counted, and a
            # multibyte character counted as one. A cap that changes size on restart is
            # not a cap.
            "SELECT COALESCE(SUM(LENGTH(payload->>'$.text')), 0) AS size "
            "FROM conversation_event WHERE execution_id = %s AND kind = %s",
            (execution_id, EVENT_KIND))
        size = (cur.fetchone() or {}).get("size") or 0
        budget = _Budget(int(row.get("n") or 0), int(size), bool(row.get("capped")))
        self._budgets[execution_id] = budget
        # One entry per execution, never removed, is unbounded memory in a consumer that
        # runs for weeks (CODE-8). Dropping the oldest is safe rather than merely cheap:
        # `_budget` reconstructs count, bytes and the truncated flag from the two tables,
        # so an evicted execution that speaks again is seeded back to exactly what it had
        # spent. The bound is on entries, not on correctness.
        while len(self._budgets) > _MAX_TRACKED_EXECUTIONS:
            self._budgets.pop(next(iter(self._budgets)))
        return budget

    # -- the two rows --------------------------------------------------------

    def _ledger(self, cur, execution_id: str, seq: int, kind: str) -> tuple[int | None, bool]:
        cur.execute(
            # INSERT IGNORE, not a pre-read: the uniqueness IS the idempotency, and a check
            # followed by an insert is the same race written out longhand.
            "INSERT IGNORE INTO execution_delta (execution_id, seq, kind) VALUES (%s, %s, %s)",
            (execution_id, seq, kind))
        if cur.rowcount == 1:
            return cur.lastrowid, True
        # A re-delivery: hand back the row that already holds this (execution, seq) so the
        # caller can still finish the work the first delivery may not have completed.
        cur.execute("SELECT id FROM execution_delta WHERE execution_id = %s AND seq = %s",
                    (execution_id, seq))
        row = cur.fetchone()
        return (row["id"] if row else None), False

    def _event(self, cur, budget: _Budget, execution_id: str, seq: int, kind: str,
               text: str | None, tool_name: str | None, *, ledger_id: int) -> None:
        if budget.conversation is None:
            cur.execute(
                "SELECT c.id FROM conversation c "
                "JOIN message m ON m.conversation_id = c.id "
                "JOIN execution e ON e.job_id = m.job_id "
                "WHERE e.execution_id = %s LIMIT 1", (execution_id,))
            row = cur.fetchone()
            if row is None:
                # Not a conversation's run, or not one YET. The ledger row still stands:
                # the cap is per execution, whoever owns it.
                return
            budget.conversation = row["id"]
        cur.execute(
            "INSERT IGNORE INTO conversation_event "
            "(conversation_id, execution_id, kind, payload, source_kind, source_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (budget.conversation, execution_id, EVENT_KIND,
             json.dumps({"seq": seq, "fragment": kind, "text": text,
                         "tool_name": tool_name}),
             SOURCE_KIND, ledger_id))
