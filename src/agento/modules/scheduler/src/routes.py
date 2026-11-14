"""The scheduler REST surface under ``/api/scheduler/`` (AG-67 plan §9).

Every route is ``acl: "self"``: the rights are per schedule (``rights.py``), so the handler
checks them. A row the user may not read is 404 (telling a user a schedule exists is
disclosure); a readable row without the right is 403; a read-only source (a Jira periodic row)
is 403 on every write. A refusal carries ``{error, code}``.

Times: ``starts_at`` (and ``local`` in a preview) is a naive local time in the row's
``timezone``, ``YYYY-MM-DDTHH:MM:SS``. ``next_fire_at``, ``last_fire_at``, ``created_at`` and a
preview's ``at`` are UTC with a ``Z``.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agento.framework.access import accounts
from agento.framework.harness.options_source import HARNESS_REGISTRY, resolve_options
from agento.framework.schedule import engine, tick
from agento.modules.conversation.src import service as conversation
from agento.web import api
from agento.web.api import Request, Response, error
from agento.web.toolbox_client import web_subject

from . import rights, store
from .rights import Rights

_NOT_FOUND = error(404, "not found")
_FORBIDDEN = error(403, "forbidden")
_FIELDS = ("title", "prompt", "agent_view_id", "rrule", "starts_at", "timezone", "harness", "model", "enabled")
MAX_TITLE = 500
MAX_MODEL = 128


def _user(req: Request) -> accounts.User:
    assert req.session is not None  # every module route is auth='session'
    return req.session.user


def _refuse(status: int, code: str, message: str | None = None) -> Response:
    return Response(status, {"error": message or code, "code": code})


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="seconds") + "Z"


def _local(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="seconds")


def _presets(conn) -> dict[str, str]:
    value = store.setting(conn, "presets")
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _label(rrule: str | None, presets: dict[str, str]) -> str | None:
    if not rrule:
        return None
    try:
        normal = engine.normalize(rrule)
    except engine.InvalidSchedule:
        return rrule
    return next((label for label, rule in presets.items() if _normal(rule) == normal), rrule)


def _normal(rule: str) -> str | None:
    try:
        return engine.normalize(rule)
    except engine.InvalidSchedule:
        return None


def _json(row: dict, can: Rights, presets: dict[str, str], extra: dict[int, dict]) -> dict:
    return {
        "id": row["id"], "source": row["source"], "source_ref": row["source_ref"], "title": row["title"],
        "prompt": row["prompt"], "agent_view_id": row["agent_view_id"], "agent_view_code": row["agent_view_code"],
        "owner_user_id": row["owner_user_id"], "owner_username": row["owner_username"],
        "owner_agent_view_id": row["owner_agent_view_id"], "rrule": row["rrule"],
        "recurrence_label": _label(row["rrule"], presets), "starts_at": _local(row["starts_at"]),
        "timezone": row["timezone"], "harness": row["harness"], "model": row["model"],
        "enabled": bool(row["enabled"]), "next_fire_at": _iso(row["next_fire_at"]),
        "last_fire_at": _iso(row["last_fire_at"]), "last_error": row["last_error"],
        "created_at": _iso(row["created_at"]), **extra.get(row["id"], {"last_run_status": None, "conversation_id": None}),
        "can": can._asdict(),
    }


def _extra(conn, ids: list[int]) -> dict[int, dict]:
    """The latest run's status and the conversation of each schedule: two reads for the page."""
    out = {i: {"last_run_status": None, "conversation_id": None} for i in ids}
    if not ids:
        return out
    marks = ",".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(f"SELECT j.schedule_id, j.status FROM job j JOIN (SELECT MAX(id) AS id FROM job"
                    f" WHERE schedule_id IN ({marks}) GROUP BY schedule_id) x ON x.id = j.id", ids)
        for r in cur.fetchall():
            out[r["schedule_id"]]["last_run_status"] = r["status"]
        cur.execute(f"SELECT s.id, c.id AS conversation_id FROM schedule s JOIN thread t"
                    f" ON t.agent_view_id = s.agent_view_id AND t.ref_hash = SHA1(CONCAT('schedule:', s.id))"
                    f" JOIN conversation c ON c.thread_id = t.id WHERE s.id IN ({marks}) AND s.source = %s",
                    (*ids, rights.SOURCE))
        for r in cur.fetchall():
            out[r["id"]]["conversation_id"] = r["conversation_id"]
    return out


def _load(req: Request, need: str) -> tuple[dict, Rights] | Response:
    with req.conn.cursor() as cur:
        cur.execute(f"{rights.ROW_SQL} WHERE s.id = %s", (int(req.params["id"]),))
        row = cur.fetchone()
    if row is None:
        return _NOT_FOUND
    can = rights.rights(req.conn, _user(req), row)
    if not can.read:
        return _NOT_FOUND
    return (row, can) if getattr(can, need) else _FORBIDDEN


def _run(fn) -> Response:
    try:
        return fn()
    except store.StoreError as exc:
        return _refuse(exc.status, exc.code, str(exc))


# --- reads -----------------------------------------------------------------------------

def list_schedules(req: Request) -> Response:
    """GET /api/scheduler/schedules: the panel probe. 403 without a read leaf or a share."""
    user = _user(req)
    if not rights.may_list(req.conn, user):
        return _FORBIDDEN
    groups = rights.list_groups(req.conn, user)
    presets = _presets(req.conn)
    extra = _extra(req.conn, [row["id"] for rows in groups.values() for row, _ in rows])
    return Response(200, {"groups": {name: [_json(row, can, presets, extra) for row, can in rows]
                                     for name, rows in groups.items()}})


def get_schedule(req: Request) -> Response:
    """GET /api/scheduler/schedules/<id>: the row; its members only with the share right."""
    loaded = _load(req, "read")
    if isinstance(loaded, Response):
        return loaded
    row, can = loaded
    body = _json(row, can, _presets(req.conn), _extra(req.conn, [row["id"]]))
    body["members"] = _members(req.conn, row["id"]) if can.share else None
    return Response(200, body)


RUNS_SHOWN = 10


def runs(req: Request) -> Response:
    """GET /api/scheduler/schedules/<id>/runs: the newest jobs of the schedule, each with the
    answer its last attempt wrote into the conversation (null before the end, or none). A reader
    of the schedule reads its conversation too, so the answer discloses nothing new; the job's
    prompt, model and tokens stay behind the admin job route."""
    loaded = _load(req, "read")
    if isinstance(loaded, Response):
        return loaded
    with req.conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.status, j.created_at, j.started_at, j.finished_at,"
            " (SELECT m.content FROM execution e JOIN message m ON m.conversation_id = e.conversation_id"
            "   AND m.execution_id = e.execution_id AND m.role = 'assistant'"
            "  WHERE e.job_id = j.id ORDER BY e.id DESC LIMIT 1) AS answer"
            " FROM job j WHERE j.schedule_id = %s ORDER BY j.id DESC LIMIT %s",
            (loaded[0]["id"], RUNS_SHOWN))
        rows = cur.fetchall()
    return Response(200, [{"id": r["id"], "status": r["status"], "created_at": _iso(r["created_at"]),
                           "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]),
                           "answer": r["answer"]} for r in rows])


def _members(conn, schedule_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT u.id AS user_id, u.username FROM schedule_member m JOIN `user` u ON u.id = m.user_id"
                    " WHERE m.schedule_id = %s ORDER BY u.username", (schedule_id,))
        return list(cur.fetchall())


def _target_views(conn, user: accounts.User) -> list[dict]:
    """The views the user may target with a new schedule (D-13): none without ``scheduler.own``."""
    if not rights.may_create(conn, user):
        return []
    return accounts.visible_agent_views(conn, user)


def options(req: Request) -> Response:
    """GET /api/scheduler/options: what the form offers."""
    user = _user(req)
    if not rights.may_list(req.conn, user):
        return _FORBIDDEN
    return Response(200, {
        "presets": [{"label": k, "rrule": v} for k, v in _presets(req.conn).items()],
        "harnesses": resolve_options(HARNESS_REGISTRY),
        "default_timezone": tick.default_timezone(req.conn),
        "min_interval_minutes": tick.min_interval_minutes(req.conn),
        "agent_views": _target_views(req.conn, user),
    })


def _parse_start(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(second=0, microsecond=0) if parsed.tzinfo is None else None


def _zone_ok(tz) -> bool:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return isinstance(tz, str)


def _now_local(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None, second=0, microsecond=0)


def _timing(conn, body: dict, base: dict) -> tuple[dict, dict] | Response:
    """The parsed timing fields of ``body`` and the full triple (``base`` fills the rest)."""
    values: dict = {}
    if "rrule" in body:
        try:
            values["rrule"] = engine.normalize(body["rrule"] if isinstance(body["rrule"], str) else "")
        except engine.InvalidSchedule as exc:
            return _refuse(400, exc.code)
        if len(values["rrule"]) > 1024:
            return _refuse(400, "bad_rule")
    if "timezone" in body:
        if not _zone_ok(body["timezone"]):
            return _refuse(400, "bad_timezone")
        values["timezone"] = body["timezone"]
    basis = {**base, **values}
    if "starts_at" in body:
        values["starts_at"] = _parse_start(body["starts_at"])
        if values["starts_at"] is None:
            return _refuse(400, "bad_start", "starts_at must be a local time YYYY-MM-DDTHH:MM")
    elif basis.get("starts_at") is None:
        values["starts_at"] = _now_local(basis["timezone"])
    return values, {**basis, **values}


def preview(req: Request) -> Response:
    """POST /api/scheduler/preview {rrule, starts_at?, timezone?}: the next 5 fires."""
    if not rights.may_list(req.conn, _user(req)):
        return _FORBIDDEN
    body = api._body(req)
    if "rrule" not in body:
        return _refuse(400, "bad_rule")
    parsed = _timing(req.conn, body, {"timezone": tick.default_timezone(req.conn)})
    if isinstance(parsed, Response):
        return parsed
    _, basis = parsed

    def go() -> Response:
        fires = store.validate(req.conn, basis["rrule"], basis["starts_at"], basis["timezone"])
        return Response(200, {"fires": [{"at": _iso(f.at_utc), "local": _local(f.local)} for f in fires]})

    return _run(go)


# --- writes ----------------------------------------------------------------------------

def _values(req: Request, body: dict, *, row: dict | None) -> tuple[dict, dict | None] | Response:
    """The checked fields of a create (``row`` None) or an update, and the timing triple to
    validate (None when no timing field is set on an update)."""
    unknown = sorted(set(body) - set(_FIELDS))
    if unknown:
        return _refuse(400, "unknown_field", f"unknown field: {unknown[0]}")
    if row is None and not all(k in body for k in ("title", "prompt", "agent_view_id", "rrule")):
        return _refuse(400, "missing_field", "title, prompt, agent_view_id and rrule are required")
    values: dict = {}
    if "title" in body:
        title = body["title"].strip() if isinstance(body["title"], str) else ""
        if not title or len(title) > MAX_TITLE:
            return _refuse(400, "bad_title", f"title must be 1 to {MAX_TITLE} characters")
        values["title"] = title
    if "prompt" in body:
        prompt = body["prompt"] if isinstance(body["prompt"], str) else ""
        limit = conversation.config(req.conn, "limits/max_message_bytes")
        if not prompt.strip() or len(prompt.encode()) > limit:
            return _refuse(400, "bad_prompt", f"prompt must be 1 to {limit} bytes")
        values["prompt"] = prompt
    if row is not None and "agent_view_id" in body:
        # The view is fixed at create: the thread, so the conversation, is keyed by it (PLN-3).
        if body["agent_view_id"] != row["agent_view_id"]:
            return _refuse(400, "view_fixed", "the agent view is set at create; delete and create again")
    elif "agent_view_id" in body:
        view_id = body["agent_view_id"]
        if not api._positive_int(view_id) or view_id not in rights.visible_ids(req.conn, _user(req)):
            return _refuse(404, "no_view", "agent_view not found")
        values["agent_view_id"] = view_id
    if "harness" in body:
        if body["harness"] is not None and body["harness"] not in {o["value"] for o in resolve_options(HARNESS_REGISTRY)}:
            return _refuse(400, "bad_harness")
        values["harness"] = body["harness"]
    if "model" in body:
        model = body["model"]
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > MAX_MODEL):
            return _refuse(400, "bad_model")
        values["model"] = model.strip() if isinstance(model, str) else None
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            return _refuse(400, "bad_enabled")
        values["enabled"] = int(body["enabled"])
    if row is not None and not any(k in body for k in store.TIMING):
        return values, None
    base = ({k: row[k] for k in store.TIMING} if row is not None
            else {"timezone": tick.default_timezone(req.conn), "starts_at": None})
    parsed = _timing(req.conn, body, base)
    if isinstance(parsed, Response):
        return parsed
    timing, basis = parsed
    return {**values, **timing}, basis


def create(req: Request) -> Response:
    """POST /api/scheduler/schedules {title, prompt, agent_view_id, rrule, starts_at?, timezone?,
    harness?, model?, enabled?}: a schedule owned by the caller (``scheduler.own``, D-13)."""
    user = _user(req)
    if not rights.may_create(req.conn, user):
        return _FORBIDDEN
    parsed = _values(req, api._body(req), row=None)
    if isinstance(parsed, Response):
        return parsed
    values, basis = parsed
    assert basis is not None

    def go() -> Response:
        fires = store.validate(req.conn, basis["rrule"], basis["starts_at"], basis["timezone"])
        limit = None if user.role == "admin" else int(store.setting(req.conn, "limits/max_per_user",
                                                                    values["agent_view_id"]))
        schedule_id = store.save(req.conn, {**values, **basis}, user=user, actor=web_subject(user), owner_user_id=user.id,
                                 fires=fires, limit=limit)
        return Response(201, {"id": schedule_id})

    return _run(go)


def update(req: Request) -> Response:
    """PATCH /api/scheduler/schedules/<id> {any create field}: the write right."""
    loaded = _load(req, "write")
    if isinstance(loaded, Response):
        return loaded
    row, _ = loaded
    parsed = _values(req, api._body(req), row=row)
    if isinstance(parsed, Response):
        return parsed
    values, basis = parsed

    def go() -> Response:
        fires = None if basis is None else store.validate(req.conn, basis["rrule"], basis["starts_at"],
                                                          basis["timezone"])
        store.save(req.conn, values, user=_user(req), actor=web_subject(_user(req)), schedule_id=row["id"],
                   basis=basis, fires=fires)
        return Response(204)

    return _run(go)


def delete(req: Request) -> Response:
    """DELETE /api/scheduler/schedules/<id>: the delete right. The conversation stays, retitled."""
    loaded = _load(req, "delete")
    if isinstance(loaded, Response):
        return loaded

    def go() -> Response:
        store.delete(req.conn, loaded[0]["id"], user=_user(req), actor=web_subject(_user(req)))
        return Response(204)

    return _run(go)


def run_now(req: Request) -> Response:
    """POST /api/scheduler/schedules/<id>/run: one fire now (D-21), as the tick fires it.
    201 {job_id, conversation_id}; 409 {code: vetoed | no_reach | overlap | too_soon}."""
    loaded = _load(req, "write")
    if isinstance(loaded, Response):
        return loaded

    def go() -> Response:
        outcome, job_id, conversation_id = store.run_now(req.conn, loaded[0]["id"], user=_user(req),
                                                         actor=web_subject(_user(req)))
        if job_id is None:
            return _refuse(409, outcome.value, "the schedule did not run")
        return Response(201, {"job_id": job_id, "conversation_id": conversation_id})

    return _run(go)


def member_options(req: Request) -> Response:
    """GET /api/scheduler/schedules/<id>/member-options: the active users a sharer may add."""
    loaded = _load(req, "share")
    if isinstance(loaded, Response):
        return loaded
    with req.conn.cursor() as cur:
        cur.execute("SELECT id, username FROM `user` WHERE is_active = 1 AND id <> %s ORDER BY username",
                    (loaded[0]["owner_user_id"] or 0,))
        return Response(200, list(cur.fetchall()))


def put_member(req: Request) -> Response:
    """PUT /api/scheduler/schedules/<id>/members {username}: share for editing (D-3)."""
    loaded = _load(req, "share")
    if isinstance(loaded, Response):
        return loaded
    username = api._body(req).get("username")
    found = accounts.get_user_by_username(req.conn, username) if isinstance(username, str) else None
    if found is None or not found.is_active:
        return _refuse(404, "no_user", "no such user")

    def go() -> Response:
        store.share(req.conn, loaded[0]["id"], found.id, user=_user(req), added=True, actor=web_subject(_user(req)))
        return Response(204)

    return _run(go)


def delete_member(req: Request) -> Response:
    """DELETE /api/scheduler/schedules/<id>/members/<user_id>."""
    loaded = _load(req, "share")
    if isinstance(loaded, Response):
        return loaded

    def go() -> Response:
        changed = store.share(req.conn, loaded[0]["id"], int(req.params["user_id"]), user=_user(req), added=False,
                              actor=web_subject(_user(req)))
        return Response(204) if changed else _NOT_FOUND

    return _run(go)


def change_owner(req: Request) -> Response:
    """POST /api/scheduler/schedules/<id>/owner {user_id}: admin only (D-12, D-14)."""
    loaded = _load(req, "change_owner")
    if isinstance(loaded, Response):
        return loaded
    user_id = api._body(req).get("user_id")
    if not api._positive_int(user_id):
        return _refuse(400, "bad_user", "user_id must be a positive integer")

    def go() -> Response:
        store.change_owner(req.conn, loaded[0]["id"], user_id, user=_user(req), actor=web_subject(_user(req)))
        return Response(204)

    return _run(go)
