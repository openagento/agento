"""The writes of the scheduler panel (AG-67 plan §8.3, §9): save, delete, share, owner change
and Run now. Each is one transaction that locks the schedule row first (``tick.lock_row``), so
one lock order holds everywhere: the schedule row, then its members, then its conversation.
Every event is dispatched after the commit (EVT-4), except the ``_before`` vetoes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agento.framework.access import accounts
from agento.framework.config_resolver import load_db_overrides, read_config_defaults, resolve_field
from agento.framework.event_manager import get_event_manager
from agento.framework.events import (
    ScheduleOwnerChangedEvent,
    ScheduleSaveBeforeEvent,
    ScheduleSavedEvent,
    ScheduleShareEvent,
)
from agento.framework.schedule import engine, tick
from agento.framework.schedule.tick import FireOutcome
from agento.framework.scoped_config import build_scoped_overrides
from agento.modules.conversation.src import service as conversation

from . import rights
from .rights import SOURCE
from .source import thread_id

MODULE_DIR = Path(__file__).resolve().parent.parent
TIMING = ("rrule", "starts_at", "timezone")
# The columns a save may write: everything else is the tick's, the owner change's or the row's id.
WRITABLE = ("title", "prompt", "agent_view_id", *TIMING, "harness", "model", "enabled")
# A GET_LOCK name is server-wide: the database name keeps two deployments on one server apart.
# The schedule_create tool takes the same name (toolbox/schedule-tools.js).
LIMIT_LOCK = "scheduler:limit:user:{}:"


class StoreError(Exception):
    """A refused write. ``status`` is the HTTP answer, ``code`` a stable reason for the panel."""

    def __init__(self, status: int, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.status, self.code = status, code


def setting(conn, path: str, agent_view_id: int | None = None):
    """``scheduler/<path>`` through the 3-level fallback, at the view's scope when given."""
    overrides = load_db_overrides(conn)
    if agent_view_id is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM agent_view WHERE id = %s", (agent_view_id,))
            view = cur.fetchone()
        overrides = build_scoped_overrides(conn, agent_view_id, view["workspace_id"] if view else None)
    schema = json.loads((MODULE_DIR / "system.json").read_text())[path]
    return resolve_field("scheduler", path, schema, read_config_defaults(MODULE_DIR), overrides).value


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def validate(conn, rrule: str, starts_at: datetime, tz: str) -> list[engine.Fire]:
    """The next fires of a rule, or ``StoreError(400, <engine code>)``. Holds no lock: the
    child process may take up to ``VALIDATE_DEADLINE_S``."""
    try:
        return engine.validate(rrule, starts_at, tz, min_interval_minutes=tick.min_interval_minutes(conn),
                               now=now_utc())
    except engine.InvalidSchedule as exc:
        raise StoreError(400, exc.code) from None


def audit(cur, schedule_id: int, action: str, actor: str, detail: dict | None = None) -> int:
    cur.execute("INSERT INTO schedule_audit (schedule_id, action, actor, detail) VALUES (%s, %s, %s, %s)",
                (schedule_id, action, actor, json.dumps(detail) if detail is not None else None))
    return cur.lastrowid


def _locked(cur, schedule_id: int, user: accounts.User, need: str) -> dict:
    """The row, locked, if ``user`` holds ``need`` on it now (SEC-7). The route's check came
    before the lock, so it does not count: a share, an owner or a role can change between the
    two. The rollback ends the route's read snapshot (REPEATABLE READ), so the reads after the
    lock see every commit made before it. Answers as the routes do: no read is 404."""
    cur.connection.rollback()
    row = tick.lock_row(cur, schedule_id)
    fresh = accounts.get_user(cur.connection, user.id)
    if row is None or fresh is None or not (can := rights.rights(cur.connection, fresh, row)).read:
        raise StoreError(404, "not_found", "not found")
    if row["source"] != SOURCE:
        raise StoreError(403, "read_only", "this schedule is read-only")
    if not getattr(can, need):
        raise StoreError(403, "forbidden", "forbidden")
    return row


def _veto(row: dict, schedule_id: int | None, actor: str) -> None:
    before = ScheduleSaveBeforeEvent(schedule_id=schedule_id, source=SOURCE, agent_view_id=row["agent_view_id"],
                                     owner_user_id=row["owner_user_id"],
                                     owner_agent_view_id=row["owner_agent_view_id"], actor=actor)
    # A set veto or a failed observer refuses the save (fail closed, SEC-9).
    if not get_event_manager().dispatch_with_result("schedule_save_before", before) or before.veto:
        raise StoreError(409, "vetoed", "the save was refused")


def save(conn, values: dict, *, user: accounts.User, actor: str, schedule_id: int | None = None, owner_user_id: int | None = None,
         basis: dict | None = None, fires: list[engine.Fire] | None = None, limit: int | None = None) -> int:
    """Create (``schedule_id`` None, owned by ``owner_user_id``) or update one row.

    ``fires`` were validated from ``basis`` (the full timing triple) before this call. A
    changed rrule, starts_at or timezone writes ``next_fire_at = fires[0]`` and clears
    ``anchor_local`` and ``last_error`` (plan §5.1); an edit that changes no timing field
    leaves them, except a re-enable, which clears ``next_fire_at`` so the tick computes it and
    a missed fire is not run late (D-5). ``limit`` caps the owner's rows on create (None: no
    limit). Dispatches ``schedule_save_before`` (veto) and, after the commit,
    ``schedule_save_after``.
    """
    lock = None
    try:
        with conn.cursor() as cur:
            if schedule_id is None:
                row = {"agent_view_id": values["agent_view_id"], "owner_user_id": owner_user_id,
                       "owner_agent_view_id": None}
                if limit is not None:
                    # End the route's read snapshot (REPEATABLE READ) first: a count from it
                    # misses a create committed while this one waited for the lock.
                    conn.rollback()
                    lock = LIMIT_LOCK.format(owner_user_id)
                    cur.execute("SELECT GET_LOCK(CONCAT(%s, DATABASE()), 10) AS got", (lock,))
                    if cur.fetchone()["got"] != 1:
                        raise StoreError(503, "busy", "try again")
                    cur.execute("SELECT COUNT(*) AS n FROM schedule WHERE owner_user_id = %s AND deleted_at IS NULL",
                                (owner_user_id,))
                    if cur.fetchone()["n"] >= limit:
                        raise StoreError(409, "limit", f"at most {limit} schedules per user")
                _veto(row, None, actor)
                if not fires:
                    raise StoreError(400, "no_occurrence")
                cols = {**values, "source": SOURCE, "owner_user_id": owner_user_id,
                        "next_fire_at": fires[0].at_utc}
                cur.execute(f"INSERT INTO schedule ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                            tuple(cols.values()))
                schedule_id = cur.lastrowid
                audit(cur, schedule_id, "create", actor)
                created = True
            else:
                row = _locked(cur, schedule_id, user, "write")
                changes = {k: v for k, v in values.items() if row[k] != v}
                if "agent_view_id" in changes:
                    raise StoreError(400, "view_fixed", "the agent view is set at create")
                if any(k in changes for k in TIMING):
                    if basis is None or fires is None or any(row[k] != basis[k] for k in TIMING if k not in values):
                        raise StoreError(409, "changed", "the schedule changed meanwhile; reload it")
                    changes.update(next_fire_at=fires[0].at_utc, anchor_local=None, last_error=None)
                elif changes.get("enabled") and not row["enabled"]:
                    changes.update(next_fire_at=None, anchor_local=None, last_error=None)
                _veto({**row, **changes}, schedule_id, actor)
                if changes:
                    cur.execute(f"UPDATE schedule SET {', '.join(f'{k} = %s' for k in changes)} WHERE id = %s",
                                (*changes.values(), schedule_id))
                    audit(cur, schedule_id, "update", actor, {"fields": sorted(k for k in changes if k in WRITABLE)})
                created = False
            agent_view_id = values.get("agent_view_id", row["agent_view_id"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if lock is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(CONCAT(%s, DATABASE()))", (lock,))
            conn.commit()
    get_event_manager().dispatch("schedule_save_after", ScheduleSavedEvent(
        schedule_id=schedule_id, source=SOURCE, agent_view_id=agent_view_id, actor=actor, created=created))
    return schedule_id


def delete(conn, schedule_id: int, *, user: accounts.User, actor: str) -> None:
    """Soft delete and finalize in one transaction (D-7): the source retitles the conversation
    (``on_delete``), the row goes, and ``schedule_delete_after`` follows the commit."""
    try:
        with conn.cursor() as cur:
            _locked(cur, schedule_id, user, "delete")
            cur.execute("UPDATE schedule SET enabled = 0, deleted_at = UTC_TIMESTAMP() WHERE id = %s", (schedule_id,))
            audit(cur, schedule_id, "delete", actor)
            event = tick.finalize_delete(cur, schedule_id, actor=actor)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if event is not None:
        get_event_manager().dispatch("schedule_delete_after", event)


def _active_user(cur, user_id: int) -> bool:
    cur.execute("SELECT 1 FROM `user` WHERE id = %s AND is_active = 1", (user_id,))
    return cur.fetchone() is not None


def share(conn, schedule_id: int, user_id: int, *, user: accounts.User, added: bool, actor: str) -> bool:
    """Add or remove one member (edit only, D-3). False: nothing changed."""
    try:
        with conn.cursor() as cur:
            row = _locked(cur, schedule_id, user, "share")
            if added:
                if not _active_user(cur, user_id):
                    raise StoreError(404, "no_user", "no such user")
                if user_id == row["owner_user_id"]:
                    raise StoreError(400, "owner", "the owner is not a member")
                cur.execute("INSERT IGNORE INTO schedule_member (schedule_id, user_id) VALUES (%s, %s)",
                            (schedule_id, user_id))
            else:
                cur.execute("DELETE FROM schedule_member WHERE schedule_id = %s AND user_id = %s",
                            (schedule_id, user_id))
            changed = cur.rowcount == 1
            if changed:
                audit(cur, schedule_id, "share" if added else "unshare", actor, {"user_id": user_id})
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if changed:
        get_event_manager().dispatch("schedule_share_after", ScheduleShareEvent(
            schedule_id=schedule_id, user_id=user_id, added=added, actor=actor))
    return changed


def change_owner(conn, schedule_id: int, user_id: int, *, user: accounts.User, actor: str) -> None:
    """Give the schedule to a user (admin only, D-12, D-14; not limited). The new owner's
    share row goes, and the schedule's conversation moves to them (D-32)."""
    try:
        with conn.cursor() as cur:
            row = _locked(cur, schedule_id, user, "change_owner")
            if not _active_user(cur, user_id):
                raise StoreError(404, "no_user", "no such user")
            cur.execute("UPDATE schedule SET owner_user_id = %s, owner_agent_view_id = NULL WHERE id = %s",
                        (user_id, schedule_id))
            cur.execute("DELETE FROM schedule_member WHERE schedule_id = %s AND user_id = %s", (schedule_id, user_id))
            audit(cur, schedule_id, "owner_change", actor, {
                "old_owner_user_id": row["owner_user_id"], "old_owner_agent_view_id": row["owner_agent_view_id"],
                "new_owner_user_id": user_id})
            tid = thread_id(cur, row)
            if tid is not None:
                conversation.set_channel_owner(cur, tid, user_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    get_event_manager().dispatch("schedule_owner_change_after", ScheduleOwnerChangedEvent(
        schedule_id=schedule_id, old_owner_user_id=row["owner_user_id"],
        old_owner_agent_view_id=row["owner_agent_view_id"], new_owner_user_id=user_id, actor=actor))


def run_now(conn, schedule_id: int, *, user: accounts.User, actor: str) -> tuple[FireOutcome, int | None, int | None]:
    """Fire once now through ``tick.fire`` (D-21, EVT-2): the same lock, checks and events as
    the tick. Writes ``last_fire_at``, never ``anchor_local``. Returns the outcome, and on
    PUBLISHED the job and its conversation."""
    try:
        with conn.cursor() as cur:
            _locked(cur, schedule_id, user, "write")
            audit_id = audit(cur, schedule_id, "run_now", actor)
            outcome, job_id, run_after = tick.fire(cur, schedule_id, fire_at=now_utc(), actor=actor,
                                                   idempotency_key=f"schedule:{schedule_id}:run:{audit_id}")
            if outcome is not FireOutcome.PUBLISHED:
                conn.rollback()
                return outcome, None, None
            cur.execute("SELECT j.thread_id, s.agent_view_id, s.owner_user_id, s.title FROM job j"
                        " JOIN schedule s ON s.id = j.schedule_id WHERE j.id = %s", (job_id,))
            found = cur.fetchone()
            conversation_id = conversation.ensure_channel_conversation(
                cur, thread_id=found["thread_id"], agent_view_id=found["agent_view_id"], channel=SOURCE,
                external_ref=str(schedule_id), user_id=found["owner_user_id"], title=found["title"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if run_after is not None:
        run_after.dispatch()
    return outcome, job_id, conversation_id
