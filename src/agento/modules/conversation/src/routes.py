"""The conversation REST surface (PRD E3-E5 §4.1, §9).

Every handler resolves the thread through `service.load_visible`, so a caller who cannot
reach it gets 404 - never 403, which would confirm that the thread exists.

The paths live under `/api/conversation/`, the prefix the module owns. The PRD writes
`/api/conversations`; the §11 rule gives a module `/api/<its own name>/` and nothing else,
and the rule wins over the spelling.
"""
from __future__ import annotations

import re

from agento.web.api import Request, Response, error

from . import retention, service


def _iso(value) -> str | None:
    return None if value is None else value.isoformat() + "Z"


def _conversation_json(row: dict) -> dict:
    return {"id": row["id"], "agent_view_id": row["agent_view_id"], "title": row["title"],
            "status": row["status"], "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]), "channel": row["channel"],
            "external_ref": row["external_ref"],
            "last_activity_at": _iso(row["last_activity_at"]), "live": bool(row.get("live"))}


def _run_json(row: dict, details: bool) -> dict:
    """Without run details a run keeps its shape; harness, provider, credential, model, tokens
    and the job link are omitted."""
    if not details:
        return {"execution_id": row["execution_id"], "attempt": row["attempt"],
                "status": row["status"], "started_at": _iso(row["started_at"]),
                "finished_at": _iso(row["finished_at"])}
    return {"execution_id": row["execution_id"], "job_id": row["job_id"],
            "attempt": row["attempt"], "status": row["status"],
            "started_at": _iso(row["started_at"]), "finished_at": _iso(row["finished_at"]),
            "type": row["type"], "harness": row["harness"], "provider": row["provider"],
            "credential": row["credential"], "model": row["model"],
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}


def _message_json(row: dict) -> dict:
    reason = row.get("blocked_reason")
    return {"id": row["id"], "role": row["role"], "content": row["content"],
            "client_message_id": row["client_message_id"], "job_id": row["job_id"],
            "job_state": row["job_state"], "created_at": _iso(row["created_at"]),
            # A reader must be able to tell "waiting" from "stuck" (§4.5).
            "blocked": reason is not None, "blocked_reason": reason}


def _visible(req: Request) -> dict | None:
    return service.load_visible(req.conn, conversation_id=int(req.params["id"]),
                                user=req.session.user)


def create(req: Request) -> Response:
    body = req.json if isinstance(req.json, dict) else {}
    view_id = body.get("agent_view_id")
    if not (isinstance(view_id, int) and not isinstance(view_id, bool) and view_id > 0):
        return error(400, "agent_view_id must be a positive integer")
    try:
        title = service.check_title(body.get("title"))
    except service.SubmissionError as exc:
        return error(exc.status, str(exc))

    user = req.session.user
    # The gate the reads use, applied before the row exists: a thread may not be created
    # against a scope its owner could not then read.
    with req.conn.cursor() as cur:
        cur.execute("SELECT workspace_id FROM agent_view WHERE id = %s", (view_id,))
        row = cur.fetchone()
    if row is None:
        return error(404, "not found")
    from agento.framework.access.accounts import can_reach, scope_is_active
    if not can_reach(req.conn, user, workspace_id=row["workspace_id"], agent_view_id=view_id) \
            or not scope_is_active(req.conn, view_id):
        return error(404, "not found")

    conversation_id = service.create_conversation(
        req.conn, user_id=user.id, agent_view_id=view_id, title=title)
    return Response(201, {"id": conversation_id})


def index(req: Request) -> Response:
    """`?scope=mine` (default) or `?scope=channels[&channel=<source>]` - admins only."""
    scope = req.query.get("scope", "mine")
    if scope not in ("mine", "channels"):
        return error(400, "scope must be mine or channels")
    before = None
    if req.query.get("before"):
        if scope != "channels" or not re.fullmatch(r"[0-9]{1,12}:[0-9]{1,19}", req.query["before"]):
            return error(400, "before must be a cursor from a channels page")
        ts, _, rid = req.query["before"].partition(":")
        before = (int(ts), int(rid))
    limit = service.config(req.conn, "history/page_size")
    rows = service.list_visible(req.conn, user=req.session.user, limit=limit,
                                channels=scope == "channels",
                                channel=req.query.get("channel") or None, before=before)
    body = [_conversation_json(r) for r in rows]
    if scope == "channels":
        for item, row in zip(body, rows, strict=True):
            item["cursor"] = service.channel_cursor(row)
    return Response(200, body)


def show(req: Request) -> Response:
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    body = _conversation_json(
        dict(row, live=service.is_live(req.conn, conversation_id=row["id"])))
    details = service.can_see_run_details(req.conn, req.session.user, row)
    body["run_details"] = details
    # The panel shows its channel composer only for a reader who may post (the route
    # checks the same grant anyway; this keeps a control the reader cannot use off screen).
    if row["user_id"] is None:
        body["channel_write"] = service.can_write_channel(req.conn, req.session.user, row)
    body["runs"] = [_run_json(r, details)
                    for r in service.list_runs(req.conn, conversation_id=row["id"])]
    return Response(200, body)


def destroy(req: Request) -> Response:
    """Archive, not delete: §10.1's deletion is an operator path with its own ordering."""
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    service.archive(req.conn, row["id"], actor_id=req.session.user.id, reason="manual")
    return Response(200, {"id": row["id"], "status": "archived"})


def messages(req: Request) -> Response:
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    limit = service.config(req.conn, "history/page_size")
    rows = service.list_messages(req.conn, conversation_id=row["id"], limit=limit)
    return Response(200, [_message_json(r) for r in rows])


def events(req: Request) -> Response:
    """Replay the thread's events after a cursor (§6.4).

    The same `load_visible` gate as every other read, so a caller who lost reach replays
    nothing: a stream that outlived its grant would be a read the panel never re-checks.
    """
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    # `0` for an absent `after`, and it IS asked of the watermark - unlike the live stream,
    # where no cursor means "start at the newest" and can therefore lose nothing. Here no
    # cursor means "from the beginning", which is a claim on exactly the rows a prune took,
    # so the survivors would be a gap presented as complete history.
    after = req.query.get("after", "0")
    if not after.isdigit():
        return error(400, "after must be a non-negative integer")
    if retention.cursor_expired(req.conn, conversation_id=row["id"], cursor=int(after)):
        # The rows this caller asked to continue from are gone. Answering the survivors
        # would be a gap presented as complete history (§10.1).
        return error(409, "cursor_expired")
    rows = service.list_events(req.conn, conversation_id=row["id"], after_id=int(after),
                               limit=service.config(req.conn, "history/page_size"))
    return Response(200, service.project_events(req.conn, rows, req.session.user, row))


def timeline(req: Request) -> Response:
    """One page of the thread's timeline, newest first in paging, oldest first in the body.

    `before` is the oldest id the client holds; without it the page is the newest. A
    `before` at or below the prune watermark is 409, like replay: the page under it is gone.
    """
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    before, limit = req.query.get("before"), req.query.get("limit")
    if (before is not None and not before.isdigit()) or (limit is not None and not limit.isdigit()):
        return error(400, "before and limit must be non-negative integers")
    if before is not None and retention.cursor_expired(
            req.conn, conversation_id=row["id"], cursor=int(before)):
        return error(409, "cursor_expired")
    rows, has_older = service.list_timeline(
        req.conn, conversation_id=row["id"],
        before_id=None if before is None else int(before),
        limit=int(limit) if limit else service.config(req.conn, "history/page_size"))
    return Response(200, {"events": service.project_events(req.conn, rows, req.session.user, row),
                          "has_older": has_older,
                          "newest_id": rows[-1]["id"] if rows else None})


def unblock(req: Request) -> Response:
    """Free a thread stuck on an unrecoverable paused turn (§4.5).

    Whoever can READ the thread may free it: it grants nothing new, it only lets the thread
    move again. The refusals are 409 - the caller can see the thread, so hiding the reason
    would leave them with a thread that is stuck for no stated cause.
    """
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    try:
        changed = service.unblock(req.conn, conversation_id=row["id"],
                                  message_id=int(req.params["message_id"]),
                                  actor_id=req.session.user.id)
    except service.SubmissionError as exc:
        return error(exc.status, str(exc))
    return Response(200, {"message_id": int(req.params["message_id"]),
                          "job_state": "terminal", "changed": changed})


def _write_refusal(req: Request, row: dict) -> Response | None:
    """What refuses a write into this thread, for EVERY write route (CLS-1).

    A channel thread mirrors a Jira issue or a mailbox. A post into it continues that
    external task as a follow-up (`complete_pending` picks the contract), so it needs the
    grant that speaks for the operator - and something to reply to. A view that is gone
    leaves readable history with nothing left to run the turn on.
    """
    if row["user_id"] is None:
        if not service.can_write_channel(req.conn, req.session.user, row):
            return error(403, "forbidden")
        if not row["external_ref"]:
            return error(409, "read_only")
    if row["agent_view_id"] is None:
        return error(404, "not found")
    return None


def regenerate(req: Request) -> Response:
    """Re-ask a user message as a new turn (§4.1's submission, with content from a row)."""
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    refusal = _write_refusal(req, row)
    if refusal is not None:
        return refusal
    body = req.json if isinstance(req.json, dict) else {}
    try:
        client_message_id = service.check_client_message_id(body.get("client_message_id"))
        message_id = body.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1:
            raise service.SubmissionError(400, "message_id must be a positive integer")
        new_id, job_id, created = service.regenerate(
            req.conn, conversation_id=row["id"], message_id=message_id,
            user_id=req.session.user.id, client_message_id=client_message_id)
    except service.SubmissionError as exc:
        return error(exc.status, str(exc))
    return Response(201 if created else 200, {"message_id": new_id, "job_id": job_id})


def send(req: Request) -> Response:
    row = _visible(req)
    if row is None:
        return error(404, "not found")
    refusal = _write_refusal(req, row)
    if refusal is not None:
        return refusal
    body = req.json if isinstance(req.json, dict) else {}
    try:
        client_message_id = service.check_client_message_id(body.get("client_message_id"))
        content = service.check_content(req.conn, body.get("content"))
        # A post into an archived thread REACTIVATES it (§3.2) - it is not refused. The
        # submission is validated first, so a rejected body does not revive a thread the
        # caller never managed to post into; the revival then happens INSIDE the insert's
        # own transaction, so retention cannot archive the thread between the two.
        message_id, job_id, created = service.submit_message(
            req.conn, conversation_id=row["id"], user_id=req.session.user.id,
            client_message_id=client_message_id, content=content,
            reactivate_actor_id=req.session.user.id)
    except service.SubmissionError as exc:
        return error(exc.status, str(exc))
    return Response(201 if created else 200,
                    {"message_id": message_id, "job_id": job_id})
