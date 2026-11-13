"""The schedule tick (AG-67 plan §5.3): fire due ``schedule`` rows, finalize soft deletes.

``scheduler:tick`` runs every minute from the framework ``cron.json``. Every row runs in its
own transaction with one row lock at a time (SCL-1), and only rows of a registered source
are touched (MOD-1). ``fire()`` is shared by the tick and a manual Run now (EVT-2).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..access.accounts import can_reach, get_user
from ..event_manager import get_event_manager
from ..events import (
    JobPublishedEvent,
    ScheduleDeletedEvent,
    ScheduleRunBeforeEvent,
    ScheduleRunEvent,
)
from ..job_models import OPEN_STATUSES
from ..job_types import resolve_job_type
from ..publish_service import publish_job_on
from . import engine, sources
from .engine import InvalidSchedule

logger = logging.getLogger(__name__)

# A named lock is server-wide: the database name keeps two deployments on one server apart.
TICK_LOCK = "scheduler:tick:"
TICK_ACTOR = "service:cron:scheduler"
DEFAULT_BATCH = 500
_TIMING = ("rrule", "starts_at", "timezone")


class FireOutcome(Enum):
    PUBLISHED = "published"
    VETOED = "vetoed"
    NO_REACH = "no_reach"
    OVERLAP = "overlap"
    TOO_SOON = "too_soon"


@dataclass(frozen=True)
class RunAfter:
    """What a PUBLISHED fire owes after its commit (EVT-4: a rollback emits nothing)."""

    run: ScheduleRunEvent
    published: JobPublishedEvent

    def dispatch(self) -> None:
        em = get_event_manager()
        em.dispatch("job_publish_after", self.published)
        em.dispatch("schedule_run_after", self.run)


@dataclass
class TickResult:
    lock_held: bool = False  # another tick runs: this one did nothing
    finalized: int = 0
    published: int = 0
    skipped: int = 0  # due rows fired with another outcome (each still advanced)
    filled: int = 0   # rows whose next_fire_at was computed
    disabled: int = 0
    failed: int = 0


@dataclass(frozen=True)
class TickConfig:
    """The process knob (CFG-1): ``AGENTO_SCHEDULER_TICK_BATCH``, rows per phase per tick."""

    batch: int = DEFAULT_BATCH

    @classmethod
    def from_env(cls) -> TickConfig:
        return cls(batch=max(1, int(os.environ.get("AGENTO_SCHEDULER_TICK_BATCH", DEFAULT_BATCH))))


def _core_setting(conn, path: str):
    from ..bootstrap import CORE_MODULES_DIR
    from ..config_resolver import load_db_overrides, read_config_defaults, resolve_field

    core = Path(CORE_MODULES_DIR) / "core"
    schema = json.loads((core / "system.json").read_text())[path]
    return resolve_field("core", path, schema, read_config_defaults(core), load_db_overrides(conn)).value


def min_interval_minutes(conn) -> int:
    """``core/scheduler/min_interval_minutes``: the smallest gap between two fires of a schedule."""
    return int(_core_setting(conn, "scheduler/min_interval_minutes"))


def default_timezone(conn) -> str:
    """``core/scheduler/default_timezone``; empty -> the ``TZ`` env -> ``UTC``. An invalid value
    logs one warning and gives ``UTC``."""
    tz = str(_core_setting(conn, "scheduler/default_timezone") or "") or os.environ.get("TZ", "") or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("scheduler default timezone %r is not an IANA zone; using UTC", tz)
        return "UTC"
    return tz


def lock_row(cur, schedule_id: int) -> dict | None:
    """``SELECT … FOR UPDATE`` of any existing row, in the caller's transaction; None if gone."""
    cur.execute("SELECT * FROM schedule WHERE id = %s FOR UPDATE", (schedule_id,))
    return cur.fetchone()


def _skip(schedule_id: int, reason: str) -> None:
    logger.info("schedule_fire_skipped id=%s reason=%s", schedule_id, reason)


def _owner_reaches(cur, row: dict) -> bool:
    if row["agent_view_id"] is None:
        return False  # no target view: fail closed (SEC-9)
    # An inactive view or workspace never runs, whoever owns the row (SEC-9; the old
    # crontab renderer filtered the same way).
    cur.execute(
        "SELECT v.workspace_id FROM agent_view v JOIN workspace w ON w.id = v.workspace_id"
        " WHERE v.id = %s AND v.is_active = 1 AND w.is_active = 1",
        (row["agent_view_id"],),
    )
    view = cur.fetchone()
    if view is None:
        return False
    if row["owner_user_id"] is None:
        return True
    conn = cur.connection
    user = get_user(conn, row["owner_user_id"])
    if user is None or not user.is_active:
        return False
    return can_reach(conn, user, workspace_id=view["workspace_id"], agent_view_id=row["agent_view_id"])


def fire(cur, schedule_id: int, *, fire_at: datetime, idempotency_key: str, actor: str,
         min_interval: int | None = None
         ) -> tuple[FireOutcome, int | None, RunAfter | None]:
    """One fire of a schedule, in the caller's transaction. Shared by the tick and Run now.

    Locks the row first, and decides only from the locked row: two callers serialize on it,
    and the second sees the first's open job (OVERLAP) or its ``last_fire_at`` (TOO_SOON).
    The caller commits, then calls ``RunAfter.dispatch()`` (PUBLISHED only). ``fire_at`` is
    naive UTC. ``min_interval`` (minutes) defaults to the core setting.
    """
    row = lock_row(cur, schedule_id)
    if row is None or row["deleted_at"] is not None or not row["enabled"]:
        _skip(schedule_id, "gone")
        return FireOutcome.NO_REACH, None, None
    source = sources.get_source(row["source"])
    if source is None:
        _skip(schedule_id, "source")
        return FireOutcome.NO_REACH, None, None
    if not _owner_reaches(cur, row):
        _skip(schedule_id, "reach")
        return FireOutcome.NO_REACH, None, None
    placeholders = ", ".join(["%s"] * len(OPEN_STATUSES))
    cur.execute(f"SELECT 1 FROM job WHERE schedule_id = %s AND status IN ({placeholders}) LIMIT 1",
                (schedule_id, *OPEN_STATUSES))
    if cur.fetchone() is not None:
        _skip(schedule_id, "overlap")
        return FireOutcome.OVERLAP, None, None
    if min_interval is None:
        min_interval = min_interval_minutes(cur.connection)
    if row["last_fire_at"] is not None and fire_at - row["last_fire_at"] < timedelta(minutes=min_interval):
        _skip(schedule_id, "too_soon")
        return FireOutcome.TOO_SOON, None, None

    before = ScheduleRunBeforeEvent(schedule_id=schedule_id, source=row["source"],
                                    agent_view_id=row["agent_view_id"], fire_at=fire_at, actor=actor)
    if not get_event_manager().dispatch_with_result("schedule_run_before", before) or before.veto:
        _skip(schedule_id, "veto")
        return FireOutcome.VETOED, None, None

    spec = source.fire_spec(cur, row)
    job_id, published = publish_job_on(
        cur, source=spec.source, agent_type=resolve_job_type(spec.agent_type),
        agent_view_id=row["agent_view_id"], reference_id=spec.reference_id,
        idempotency_key=idempotency_key, requester=spec.requester, priority=spec.priority,
        context=spec.context, thread=spec.thread, harness=row["harness"], model=row["model"],
        schedule_id=schedule_id,
    )
    cur.execute("UPDATE schedule SET last_fire_at = %s WHERE id = %s", (fire_at, schedule_id))
    if published is None:  # this key already published its job: nothing new to announce
        return FireOutcome.PUBLISHED, job_id, None
    run = ScheduleRunEvent(schedule_id=schedule_id, job_id=job_id, source=row["source"],
                           agent_view_id=row["agent_view_id"], fire_at=fire_at, actor=actor)
    return FireOutcome.PUBLISHED, job_id, RunAfter(run=run, published=published)


def finalize_delete(cur, schedule_id: int, *, actor: str) -> ScheduleDeletedEvent | None:
    """Remove a soft-deleted row, in the caller's transaction. Returns the
    ``schedule_delete_after`` payload, which the CALLER dispatches after its commit.

    Acts only when the locked row has ``deleted_at`` set (else None: already finalized, or
    restored). Calls the source's optional ``on_delete(cur, row)`` first.
    """
    row = lock_row(cur, schedule_id)
    if row is None or row["deleted_at"] is None:
        return None
    source = sources.get_source(row["source"])
    # on_delete is optional: a source without it keeps loading (CODE-3).
    on_delete = getattr(source, "on_delete", None)
    if on_delete is not None:
        on_delete(cur, row)
    cur.execute("DELETE FROM schedule WHERE id = %s", (schedule_id,))
    return ScheduleDeletedEvent(schedule_id=schedule_id, source=row["source"],
                                agent_view_id=row["agent_view_id"], actor=actor)


def _has_count(rrule: str) -> bool:
    return "COUNT" in (part.partition("=")[0] for part in engine.normalize(rrule).split(";"))


def _fill(conn, schedule_id: int, *, now: datetime, min_interval: int, tz_default: str, result: TickResult) -> None:
    """A row with no next_fire_at: validate (child process, no lock held), then set or disable."""
    with conn.cursor() as cur:
        cur.execute("SELECT rrule, starts_at, timezone FROM schedule WHERE id = %s", (schedule_id,))
        seen = cur.fetchone()
    conn.rollback()  # end the read; no lock is held during the validation
    if seen is None:
        return
    error = None
    fires: list[engine.Fire] = []
    try:
        if not seen["rrule"] or seen["starts_at"] is None:
            raise InvalidSchedule("bad_rule")
        fires = engine.validate(seen["rrule"], seen["starts_at"], seen["timezone"] or tz_default,
                                min_interval_minutes=min_interval, now=now)
    except InvalidSchedule as exc:
        error = exc.code
    with conn.cursor() as cur:
        row = lock_row(cur, schedule_id)
        if (row is None or row["next_fire_at"] is not None or row["deleted_at"] is not None
                or not row["enabled"] or any(row[k] != seen[k] for k in _TIMING)):
            conn.rollback()  # changed while it was validated: the next tick takes it
            return
        if error is not None:
            cur.execute("UPDATE schedule SET enabled = 0, last_error = %s WHERE id = %s", (error, schedule_id))
            logger.warning("schedule_disabled id=%s reason=%s", schedule_id, error)
            result.disabled += 1
        else:
            cur.execute("UPDATE schedule SET next_fire_at = %s, anchor_local = NULL, last_error = NULL "
                        "WHERE id = %s", (fires[0].at_utc, schedule_id))
            result.filled += 1
    conn.commit()


def _advance(cur, row: dict, *, now: datetime, tz: str) -> bool:
    """Move a fired row to its next occurrence (every outcome advances). True: it was disabled."""
    t = row["next_fire_at"]
    rrule, starts_at = row["rrule"], row["starts_at"]
    count = _has_count(rrule)
    anchor = row["anchor_local"]
    try:
        current = engine.next_fires(rrule, starts_at, tz, after=t - timedelta(seconds=1), n=1, anchor_local=anchor)
        if current and current[0].at_utc == t and not count:
            anchor = current[0].local  # the scheduled occurrence just handled (plan §5.1 anchor)
        upcoming = engine.next_fires(rrule, starts_at, tz, after=max(now, t), n=1, anchor_local=anchor)
    except InvalidSchedule as exc:
        upcoming, error = [], exc.code
    else:
        error = "ended"
    if upcoming:
        cur.execute("UPDATE schedule SET next_fire_at = %s, anchor_local = %s WHERE id = %s",
                    (upcoming[0].at_utc, None if count else anchor, row["id"]))
        return False
    cur.execute("UPDATE schedule SET enabled = 0, last_error = %s, next_fire_at = NULL WHERE id = %s",
                (error, row["id"]))
    logger.info("schedule_disabled id=%s reason=%s", row["id"], error)
    return True


def _fire_due(conn, schedule_id: int, *, now: datetime, min_interval: int, tz_default: str,
              result: TickResult) -> None:
    run_after = None
    with conn.cursor() as cur:
        row = lock_row(cur, schedule_id)
        if (row is None or not row["enabled"] or row["deleted_at"] is not None
                or row["next_fire_at"] is None or row["next_fire_at"] > now):
            conn.rollback()
            return
        t = row["next_fire_at"]
        outcome, _, run_after = fire(cur, schedule_id, fire_at=t, actor=TICK_ACTOR, min_interval=min_interval,
                                     idempotency_key=f"schedule:{schedule_id}:{t:%Y%m%dT%H%M}")
        if _advance(cur, row, now=now, tz=row["timezone"] or tz_default):
            result.disabled += 1
    conn.commit()
    if outcome is FireOutcome.PUBLISHED:
        result.published += 1
    else:
        result.skipped += 1
    if run_after is not None:
        run_after.dispatch()


def tick(conn, *, now: datetime, batch: int) -> TickResult:
    """Phase A finalizes soft deletes, phase B fires due rows; ``batch`` rows each at most.

    ``now`` is naive or aware UTC. Holds the named lock ``scheduler:tick:<db>`` on ``conn`` for the
    whole run; when another tick holds it, returns at once with ``lock_held``.
    """
    now = now if now.tzinfo is None else now.astimezone(UTC).replace(tzinfo=None)
    result = TickResult()
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(CONCAT(%s, DATABASE()), 0) AS got", (TICK_LOCK,))
        if cur.fetchone()["got"] != 1:
            return TickResult(lock_held=True)
    try:
        names = list(sources.load_sources())
        if not names:
            return result
        in_names = ", ".join(["%s"] * len(names))
        min_interval = min_interval_minutes(conn)
        tz_default = default_timezone(conn)

        with conn.cursor() as cur:
            cur.execute(f"SELECT id FROM schedule WHERE deleted_at IS NOT NULL AND source IN ({in_names}) "
                        "ORDER BY id LIMIT %s", (*names, batch))
            deleted = [r["id"] for r in cur.fetchall()]
        conn.rollback()
        for schedule_id in deleted:
            try:
                with conn.cursor() as cur:
                    event = finalize_delete(cur, schedule_id, actor=TICK_ACTOR)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                logger.error("schedule_tick_failed id=%s phase=delete error=%s", schedule_id, type(exc).__name__)
                result.failed += 1
                continue
            if event is not None:
                result.finalized += 1
                get_event_manager().dispatch("schedule_delete_after", event)

        with conn.cursor() as cur:
            cur.execute(f"SELECT id, next_fire_at FROM schedule WHERE enabled = 1 AND deleted_at IS NULL "
                        f"AND source IN ({in_names}) AND (next_fire_at IS NULL OR next_fire_at <= %s) "
                        "ORDER BY next_fire_at IS NOT NULL, next_fire_at, id LIMIT %s", (*names, now, batch))
            due = cur.fetchall()
        conn.rollback()
        for r in due:
            step = _fill if r["next_fire_at"] is None else _fire_due
            try:
                step(conn, r["id"], now=now, min_interval=min_interval, tz_default=tz_default, result=result)
            except Exception as exc:
                conn.rollback()
                logger.error("schedule_tick_failed id=%s error=%s", r["id"], type(exc).__name__)
                result.failed += 1
        return result
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(CONCAT(%s, DATABASE()))", (TICK_LOCK,))
        conn.commit()
