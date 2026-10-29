"""Resume the parent when all its children finish (`subagent/wake_parent`).

When a row ends (SUCCESS, blocked, or DEAD - also from stale recovery), the observer asks, for
the row and each row above it, whether it is now due for a wake. A row P is woken
once - one `followup` row with the key `subagent_wake:{P}` that continues P's line, so the
claim resumes P's session - when:

- P is SUCCESS, has a view and a reference, `wake_parent` is on for P's view, and P has a child;
- no row of P's subtree is open (TODO, RUNNING, PAUSED);
- no row of P's subtree is itself due for a wake (`pending`: it meets the first rule and has
  no wake row yet).

Every read of P's subtree stays in P's thread: a child that a `job_claim_before` observer
detached (an Outlook revocation) has left it, and so has the work it starts. P neither waits
for those rows nor names them, because `agent_result` refuses them. The walk up from an
ending row crosses a detach, because that end can be the last one P waits for.

The last rule is what makes it race-free: a row whose own children just finished is
`pending` until its wake row exists, and that wake row is open until it ends, so an ancestor
never sees "all settled" while a turn below it is still due. Two observers that decide the
same wake publish one row (the idempotency key). The reads are batched (CODE-8): per row
checked, one query per subtree level and one for the wake keys; per observer run, one read per
level above the ending row's parent and one config read per distinct view. At most four rows
are checked (depth 3), so the cost of one end has a fixed bound.
"""
from __future__ import annotations

import logging

from agento.framework.config_resolver import ScopedConfigService
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection, pooled
from agento.framework.events import JobBlockedEvent, JobDeadEvent, JobSucceededEvent
from agento.framework.job_models import OPEN_STATUSES
from agento.framework.job_types import resolve_job_type
from agento.framework.publish_service import publish_job
from agento.framework.scoped_config import Scope

logger = logging.getLogger(__name__)

CONFIG_PATH = "subagent/wake_parent"
# A root job delegates at most three levels down (agent.js MAX_DEPTH): no row is deeper, so
# a subtree has at most three levels below its top.
MAX_DEPTH = 3
_COLUMNS = ("j.id, j.parent_job_id, j.status, j.agent_view_id, j.line_id, j.source, "
            "j.reference_id, j.priority, j.thread_id, j.depth, j.dry_run, v.code AS view_code")


def wake_key(job_id: int) -> str:
    return f"subagent_wake:{job_id}"


def _in(ids) -> str:
    return ",".join(["%s"] * len(ids))


class _WakeConfig:
    """`wake_parent` per view, read once per view for one decision."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self._cache: dict[int | None, bool] = {}

    def on(self, agent_view_id: int | None) -> bool:
        if agent_view_id not in self._cache:
            svc = (ScopedConfigService(self._conn) if agent_view_id is None
                   else ScopedConfigService(self._conn, Scope.AGENT_VIEW, agent_view_id))
            value = svc.get(CONFIG_PATH)
            # The same parse as agent.js `truthy`: config.json holds the default (on), so the
            # description the agent reads and the wake agree, and unset is off.
            self._cache[agent_view_id] = str(value or "").strip().lower() in ("1", "true", "yes")
        return self._cache[agent_view_id]

    def wakeable(self, row: dict) -> bool:
        """Whether a settled row with a child gets a wake: SUCCESS, a view and a reference (a
        follow-up needs both), and `wake_parent` on for its view. One test for "is woken"
        and "is still due", so a row that is never woken never holds its ancestors."""
        return (row["status"] == "SUCCESS" and row["agent_view_id"] is not None
                and row["reference_id"] is not None and self.on(row["agent_view_id"]))


def _rows(cur, where: str, args) -> list[dict]:
    cur.execute(f"SELECT {_COLUMNS} FROM job j LEFT JOIN agent_view v ON v.id = j.agent_view_id "
                f"WHERE {where}", tuple(args))
    return list(cur.fetchall())


def wake_prompt(children: list[dict]) -> str:
    """One line per child line of P: its latest row, so a child woken in turn reports the
    outcome of its last turn."""
    latest: dict[int, dict] = {}
    for row in children:
        key = row["line_id"] or row["id"]
        if key not in latest or row["id"] > latest[key]["id"]:
            latest[key] = row
    lines = [f"#{r['id']} ({r['view_code'] or 'deleted view'}) {r['status']}"
             for r in sorted(latest.values(), key=lambda r: r["id"])]
    return "\n".join(["Sub-agents you started have finished:", *lines,
                      "Read each result with agent_result."])


def maybe_wake(conn, parent_id: int, config: _WakeConfig | None = None) -> int | None:
    """Publish the wake of row `parent_id` when it is due; return the wake job id, or None.

    `config` may be shared by the calls of one observer run, so each view is read once.
    """
    conn.rollback()  # a fresh snapshot: every row that committed before this call is seen
    config = config or _WakeConfig(conn)
    with conn.cursor() as cur:
        found = _rows(cur, "j.id = %s", (parent_id,))
        parent = found[0] if found else None
        if parent is None:
            return None
        thread = parent["thread_id"]
        children = _rows(cur, "j.parent_job_id = %s AND j.thread_id = %s", (parent_id, thread))
        if not children or not config.wakeable(parent):
            return None
        subtree, level = [], children
        while level:
            if any(r["status"] in OPEN_STATUSES for r in level):
                return None
            subtree.extend(level)
            if level[0]["depth"] >= MAX_DEPTH:  # one level has one depth
                break
            ids = [r["id"] for r in level]
            level = _rows(cur, f"j.parent_job_id IN ({_in(ids)}) AND j.thread_id = %s",
                          (*ids, thread))
        with_children = {r["parent_job_id"] for r in subtree}
        candidates = [r for r in subtree if r["id"] in with_children and config.wakeable(r)]
        if candidates:
            keys = [wake_key(r["id"]) for r in candidates]
            cur.execute(f"SELECT idempotency_key FROM job WHERE idempotency_key IN ({_in(keys)})",
                        keys)
            woken = {r["idempotency_key"] for r in cur.fetchall()}
            if any(wake_key(r["id"]) not in woken for r in candidates):
                return None
    conn.rollback()
    depth = parent["depth"]
    return publish_job(
        source=parent["source"], agent_type=resolve_job_type("followup"),
        agent_view_id=parent["agent_view_id"], reference_id=parent["reference_id"],
        idempotency_key=wake_key(parent_id), requester=None, priority=parent["priority"],
        context=wake_prompt(children), thread_id=parent["thread_id"],
        parent_job_id=parent["parent_job_id"], depth=depth,
        # At depth 0 the claim follows the thread's depth-0 line; deeper, P's own line.
        line_id=None if depth == 0 else (parent["line_id"] or parent["id"]),
        dry_run=bool(parent["dry_run"]),
    )


def _ancestors(conn, job_id: int, parent_id: int | None, depth: int) -> list[int]:
    """`job_id` and the rows above it, nearest first: one read per level above the parent
    (`depth - 1` reads; a depth-0 row and a child of a root need none).

    The walk follows every link, also one that a detach made leave the thread. The end of
    a detached row can be the last event its old parent waits for (the parent judges in
    its own thread, where that row no longer counts), and the same holds for every row
    above that parent. `maybe_wake` applies the thread filter."""
    chain = [job_id]
    with conn.cursor() as cur:
        while parent_id is not None:
            chain.append(parent_id)
            if len(chain) > depth:          # the parent is at depth 0
                break
            cur.execute("SELECT parent_job_id FROM job WHERE id = %s", (parent_id,))
            row = cur.fetchone()
            parent_id = row["parent_job_id"] if row else None
    return chain


class WakeParentObserver:
    """On a terminal outcome of row R: wake R, and each row above it, when due.

    Every ancestor, not only the parent: a row that is never woken itself (DEAD, blocked,
    `wake_parent` off in its view) can end before its own children, and then the end of
    its last child is the only event left for the rows above it. Nearest first: a wake
    published below is an open row that holds every ancestor's wake.
    """

    def execute(self, event: object) -> None:
        if not isinstance(event, (JobSucceededEvent, JobBlockedEvent, JobDeadEvent)):
            return
        job = event.job
        with pooled(DatabaseConfig.from_env(), get_connection) as conn:
            config = _WakeConfig(conn)
            for row_id in _ancestors(conn, job.id, job.parent_job_id, min(job.depth, MAX_DEPTH)):
                woken = maybe_wake(conn, row_id, config)
                if woken is not None:
                    logger.info("Sub-agents of job %s finished: wake job %s", row_id, woken)
