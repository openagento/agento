"""`conversation:backfill` — gives the jobs that ended before this module ran their place in
a conversation.

A job with no `execution` row gets one, in the conversation of its thread, through the live
writers (`link_execution`, the delta sink, the finalizer) with the job's own times: an
imported run sits in its time place, not at the end. Its run events come from the session
file when the harness can read it (`session_file` + `session_transcript`) and the file's
turns match the session's jobs one to one; else the run shows only its start, its answer
and its end (the fallback).

One transaction per job, and no lock is held between jobs. The lock order is the claim's:
the job row, then the conversation row. A second run finds an execution row and skips the
job; two runs at once collide on `uq_execution_id` and one rolls back.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agento.framework.consumer import FRAGMENT_KINDS
from agento.framework.execution_deltas import SUPERSEDED_BY
from agento.framework.execution_hooks import DeltaRecord
from agento.framework.harness.registry import find_harness
from agento.framework.job_models import TERMINAL_STATUSES
from agento.framework.persistent_home import state_dir
from agento.framework.threads import default_ref, ensure_thread

from . import service
from .deltas import ConversationDeltaSink
from .finalizer import ConversationFinalizer

log = logging.getLogger(__name__)

EXCLUDED_SOURCES = ("conversation", "subagent", "e2e")
# A memory bound: the whole session file is parsed at once.
MAX_TRANSCRIPT_BYTES = 64 << 20
PAGE = 500
OUTCOME = {"SUCCESS": "succeeded", "FAILED": "failed", "DEAD": "failed"}
_MS = timedelta(milliseconds=1)
_KINDS = FRAGMENT_KINDS - set(SUPERSEDED_BY)


@dataclass
class Report:
    """Counts per source."""
    transcript: Counter = field(default_factory=Counter)
    fallback: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    skipped: dict[str, dict[str, int]] = field(default_factory=dict)


def _filters(sources, agent_view, since):
    sql = (" FROM job j LEFT JOIN agent_view v ON v.id = j.agent_view_id"
           f" WHERE j.status IN ({', '.join(['%s'] * len(TERMINAL_STATUSES))}) AND j.depth = 0"
           f" AND j.source NOT IN ({', '.join(['%s'] * len(EXCLUDED_SOURCES))})")
    params: list = [*TERMINAL_STATUSES, *EXCLUDED_SOURCES]
    if sources:
        sql += f" AND j.source IN ({', '.join(['%s'] * len(sources))})"
        params += sources
    if agent_view:
        sql += " AND v.code = %s"
        params.append(agent_view)
    return sql, params


def run(conn, *, sources: list[str] | None, agent_view: str | None, since: datetime | None,
        limit: int | None, dry_run: bool) -> Report:
    report = Report()
    sql, params = _filters(sources, agent_view, since)
    has_execution = "EXISTS (SELECT 1 FROM execution e WHERE e.job_id = j.id)"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.source, "
            f"  SUM({has_execution}) AS has_execution, "
            f"  SUM(NOT {has_execution} AND v.id IS NULL) AS no_view, "
            f"  SUM(NOT {has_execution} AND v.id IS NOT NULL AND j.created_at < %s) AS outside_window"
            f"{sql} GROUP BY j.source", [since or datetime.min, *params])
        for row in cur.fetchall():
            report.skipped[row["source"]] = {
                k: int(row[k] or 0) for k in ("has_execution", "no_view", "outside_window")}
    conn.commit()
    # Pages by id, so no list grows with the history (CODE-8); a page is imported in thread
    # order (`_load`), which keeps one session's jobs together for the transcript cache.
    transcripts, last, taken = _Transcripts(), 0, 0
    while limit is None or taken < limit:
        size = PAGE if limit is None else min(PAGE, limit - taken)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT j.id{sql} AND v.id IS NOT NULL AND NOT {has_execution} AND j.id > %s"
                + (" AND j.created_at >= %s" if since else "") + " ORDER BY j.id LIMIT %s",
                [*params, last, *([since] if since else []), size])
            ids = [r["id"] for r in cur.fetchall()]
        conn.commit()
        if not ids:
            break
        last, taken = ids[-1], taken + len(ids)
        jobs = _load(conn, ids)
        turns = transcripts.page(conn, jobs)
        for job in jobs:
            try:
                with_transcript = _import(conn, job, turns.get(job["id"]))
            except Exception as exc:
                conn.rollback()
                report.errors[job["source"]] += 1
                log.warning("backfill: job %s not imported (%s)", job["id"], type(exc).__name__)
                continue
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            (report.transcript if with_transcript else report.fallback)[job["source"]] += 1
    return report


def _load(conn, ids: list[int]) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.source, j.reference_id, j.agent_view_id, j.agent_type, j.provider, "
            "  j.model, j.attempt, j.status, j.session_id, j.thread_id, j.created_at, "
            "  j.started_at, j.finished_at, j.updated_at, v.code AS view_code, "
            "  w.code AS workspace_code "
            "FROM job j JOIN agent_view v ON v.id = j.agent_view_id "
            "JOIN workspace w ON w.id = v.workspace_id "
            f"WHERE j.id IN ({', '.join(['%s'] * len(ids))}) "
            "ORDER BY j.agent_view_id, j.source, j.reference_id, "
            "  COALESCE(j.started_at, j.created_at), j.id", ids)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


class _Transcripts:
    """The turn of each job, read from its session file (D8). Only the last file is kept:
    the jobs of one thread are consecutive in a page."""

    def __init__(self):
        self._path = None
        self._turns = None

    def page(self, conn, jobs: list[dict]) -> dict[int, list[dict]]:
        readable = [j for j in jobs if j["session_id"] and _reader(j["agent_type"])]
        if not readable:
            return {}
        sessions = sorted({j["session_id"] for j in readable})
        # ponytail: no index on job.session_id, one scan per page of a one-shot operator
        # command; add one if imports of large tables get slow.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, session_id, agent_view_id, agent_type FROM job "
                f"WHERE session_id IN ({', '.join(['%s'] * len(sessions))}) "
                "ORDER BY COALESCE(started_at, created_at), id", sessions)
            siblings = defaultdict(list)
            for r in cur.fetchall():
                siblings[(r["session_id"], r["agent_view_id"], r["agent_type"])].append(r["id"])
        conn.commit()
        out = {}
        for job in readable:
            group = siblings[(job["session_id"], job["agent_view_id"], job["agent_type"])]
            turns = self._read(_reader(job["agent_type"]), job)
            if turns is not None and len(turns) == len(group):
                out[job["id"]] = turns[group.index(job["id"])]
        return out

    def _read(self, adapter, job: dict) -> list[list[dict]] | None:
        """A file the harness cannot read is the fallback for its jobs, never a stop."""
        try:
            path = adapter.session_file(
                state_dir(job["workspace_code"], job["view_code"]), job["session_id"])
            if path != self._path:
                self._path, self._turns = path, None
                if path is not None and path.stat().st_size <= MAX_TRANSCRIPT_BYTES:
                    self._turns = adapter.session_transcript(path)
        except Exception as exc:
            log.info("backfill: session of job %s not read (%s)", job["id"], type(exc).__name__)
            return None
        return self._turns


def _reader(harness: str | None):
    entry = find_harness(harness) if harness else None
    adapter = getattr(entry, "adapter", None)
    if hasattr(adapter, "session_file") and hasattr(adapter, "session_transcript"):
        return adapter
    return None


def _import(conn, job: dict, turn: list[dict] | None) -> bool:
    """Writes one job's run; the caller commits or rolls back. True when it had a transcript."""
    t0 = job["started_at"] or job["created_at"]
    # The order run.started < events < answer < run.finished always holds.
    t1 = max(job["finished_at"] or job["updated_at"], t0 + 3 * _MS)
    execution_id = f"backfill:{job['id']}"
    with conn.cursor() as cur:
        # The output is read here, one job at a time: a stored raw stream can be megabytes.
        cur.execute("SELECT output FROM job WHERE id = %s FOR UPDATE", (job["id"],))
        output = cur.fetchone()["output"] or ""
        if job["thread_id"] is None:
            thread_id = ensure_thread(cur, agent_view_id=job["agent_view_id"], ref=default_ref(
                job["source"], job["reference_id"], job["id"]))
            cur.execute("UPDATE job SET thread_id = %s WHERE id = %s AND thread_id IS NULL",
                        (thread_id, job["id"]))
        cur.execute(
            "INSERT INTO execution (execution_id, job_id, attempt, status, harness, provider, "
            "model, started_at) VALUES (%s, %s, %s, 'running', %s, %s, %s, %s)",
            (execution_id, job["id"], job["attempt"], job["agent_type"], job["provider"],
             job["model"], t0))
        service.link_execution(cur, execution_row_id=cur.lastrowid, execution_id=execution_id,
                               job_id=job["id"], attempt=job["attempt"], at=t0)
        records = _records(execution_id, turn or [], t0, t1)
        if records:
            ConversationDeltaSink().write_on(cur, records)
    ConversationFinalizer().finalize(
        conn=conn, job_id=job["id"], attempt=job["attempt"], execution_id=execution_id,
        outcome=OUTCOME[job["status"]], job_terminal=True, at=t1,
        answer=_answer(job["agent_type"], output))
    return turn is not None


def _answer(harness: str | None, output: str) -> str:
    """The answer in the job's output, through the harness when it reads it ("" = none)."""
    entry = find_harness(harness) if harness else None
    read = getattr(getattr(entry, "adapter", None), "stored_answer", None)
    return (read(output) if read and output else output) or ""


def _records(execution_id: str, turn: list[dict], t0: datetime, t1: datetime) -> list[DeltaRecord]:
    """The turn's fragments with their line time, held inside `[t0, t1 - 2 ms]`; a fragment
    with no time is 1 ms after the one before it."""
    records, at = [], t0
    for fragment in turn:
        kind = fragment.get("kind")
        if kind not in _KINDS:
            continue
        at = min(max(_time(fragment.get("at")) or at + _MS, t0), t1 - 2 * _MS)
        text, data = fragment.get("text"), fragment.get("data")
        records.append(DeltaRecord(
            execution_id=execution_id, seq=len(records) + 1, kind=kind,
            text=text if isinstance(text, str) else None, tool_name=fragment.get("tool_name"),
            data=data if isinstance(data, dict) else None, created_at=at))
    return records


def _time(value) -> datetime | None:
    """A session line's ISO time as naive UTC, the database's time zone."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
