"""The SSE stream (PRD E3-E5 §7.2-§7.4).

A **poll loop**, deliberately, and not in-process pub/sub: the consumer writes
`conversation_event` from the *cron* container and `web` reads in a different process, so
there is no queue the two could share. Polling a table both can see is the whole mechanism.

Three things make it safe to hold a connection open for minutes:

* **The session is re-read every tick** (§7.4). A revoked session, a deactivated user, a
  removed grant or a deactivated agent_view ends the stream on the next tick - and the tick
  re-runs `service.load_visible`, so "the stream closes" and "the read answers 404" are the
  same decision (§9) rather than two rules that drift apart.
* **`max_duration_seconds` closes the server side.** The client's own reconnect resumes from
  its last id, so a bounded stream costs nothing and an abandoned one cannot last for ever.
* **Every tick commits first.** MySQL's REPEATABLE READ would otherwise pin the first tick's
  snapshot and the loop would poll for ever without seeing a single new row.

Each event goes out with `id: <conversation_event.id>`, which is what makes reconnect
automatic: the browser resends it as `Last-Event-ID` and needs no cursor code of its own.
"""
from __future__ import annotations

import json
import threading
import time

from agento.framework.access import sessions
from agento.web.streaming import frame, sse

from . import retention, service

HEARTBEAT = b": ping\n\n"


# --- the per-user stream cap (§7.3) ----------------------------------------
#
# The budget this bounds is THREADS. `web` is a `ThreadingHTTPServer`, and one open streaming
# response costs exactly one thread: measured at 25 concurrent long-lived responses, threads
# went 2 -> 27 and back to 2 on close (delta 25, ratio 1.00). So a user's live streams are a
# user's share of the process, one for one.
#
# Exceeding the cap closes the OLDEST stream for that user and never refuses the new one. A
# refusal would make a reconnect storm self-inflicted denial of service: the client whose
# stream just dropped is precisely the one asking again, and telling it "no" leaves it with
# nothing while its own stale connections hold the budget.
#
# The registry is per process. `web` runs one today; a second replica would give each its own
# budget, which is the same ponytail note the login throttle carries.

_cap_lock = threading.Lock()
_open_streams: dict[int, list[_Slot]] = {}


class _Slot:
    """One live stream. `closed` is read by its own loop on the next tick - nothing here
    touches another thread's generator, which is what makes closing safe."""

    __slots__ = ("closed", "user_id")

    def __init__(self, user_id: int) -> None:
        self.user_id, self.closed = user_id, False


def acquire_slot(user_id: int, cap: int) -> _Slot:
    """Register a new stream and close the oldest ones over the cap. Never the new one."""
    slot = _Slot(user_id)
    with _cap_lock:
        live = _open_streams.setdefault(user_id, [])
        live.append(slot)
        # `> 1` as well as `> cap`: the stream just opened is last and must survive even a
        # cap an operator set to zero. Closed slots leave the list here rather than when
        # their own loop notices, so a reconnect storm converges on exactly `cap` and does
        # not close one extra per still-draining generator.
        while len(live) > cap and len(live) > 1:
            live.pop(0).closed = True
    return slot


def release_slot(slot: _Slot) -> None:
    with _cap_lock:
        live = _open_streams.get(slot.user_id)
        if live is None:
            return
        if slot in live:
            live.remove(slot)
        if not live:
            _open_streams.pop(slot.user_id, None)


def live_streams(user_id: int) -> int:
    with _cap_lock:
        return len(_open_streams.get(user_id, []))


def parse_cursor(raw: str | None) -> int | None:
    """The client's cursor, or None for "no cursor".

    None is **not** zero. A stream with no cursor is a live stream: it starts at the newest
    event, because a client that never asked for history must not be handed the whole thread
    the moment it connects. Replay from the beginning is the `?after=0` route's job, and a
    `Last-Event-ID` the browser garbled is "no cursor", never a `500`.
    """
    if raw is None:
        return None
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


def newest_event_id(conn, conversation_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) AS newest FROM conversation_event "
                    "WHERE conversation_id = %s", (conversation_id,))
        return int(cur.fetchone()["newest"])


def event_frame(row: dict) -> bytes:
    payload = row["payload"]
    return frame(
        data=json.dumps({
            "id": row["id"], "kind": row["kind"], "execution_id": row["execution_id"],
            "payload": json.loads(payload) if isinstance(payload, str) else payload,
        }),
        event=row["kind"],
        event_id=row["id"],
    )


def frames(conn, *, conversation_id: int, session_token: str, cursor: int | None,
           user_id: int, now=time.monotonic, sleep=time.sleep):
    """The generator the listener drains. Its `finally` is where the §7.3 slot is released -
    on a clean end, a dropped client and a handler that raised alike, because the listener
    closes the generator on every exit (see web/streaming.py)."""
    poll = service.config(conn, "stream/poll_interval_ms") / 1000.0
    heartbeat = service.config(conn, "stream/heartbeat_seconds")
    deadline = now() + service.config(conn, "stream/max_duration_seconds")
    page = service.config(conn, "history/page_size")
    last_sent = now()

    # The question is asked of the CALLER's cursor, before one is invented for it. Asking it
    # after substituting the newest id made "no cursor" expirable: a fully pruned thread has
    # no newest id, `newest_event_id` answers 0, and 0 is at or below any watermark - so a
    # live stream opened with no cursor at all was told its cursor had expired.
    if retention.cursor_expired(conn, conversation_id=conversation_id, cursor=cursor):
        # Before any event frame, and then the stream ends: a browser reconnecting after a
        # prune must be TOLD, not handed the survivors as if nothing were missing. The
        # client restarts from the newest page.
        yield frame(data="{}", event="cursor_expired")
        return
    if cursor is None:
        cursor = newest_event_id(conn, conversation_id)

    slot = acquire_slot(user_id, service.config(conn, "stream/max_per_user"))
    try:
        yield from _tick(conn, conversation_id=conversation_id, session_token=session_token,
                         cursor=cursor, slot=slot, poll=poll, heartbeat=heartbeat,
                         deadline=deadline, page=page, last_sent=last_sent,
                         now=now, sleep=sleep)
    finally:
        release_slot(slot)


def _tick(conn, *, conversation_id, session_token, cursor, slot, poll, heartbeat, deadline,
          page, last_sent, now, sleep):
    while now() < deadline and not slot.closed:
        # Without this the loop holds the first tick's snapshot and never sees a new row.
        conn.commit()
        session = sessions.lookup_session(conn, session_token)
        if session is None:
            return
        if service.load_visible(conn, conversation_id=conversation_id,
                                user=session.user) is None:
            return
        rows = service.list_events(conn, conversation_id=conversation_id,
                                   after_id=cursor, limit=page)
        for row in rows:
            cursor = row["id"]
            yield event_frame(row)
        if rows:
            last_sent = now()
        elif now() - last_sent >= heartbeat:
            yield HEARTBEAT
            last_sent = now()
        sleep(poll)


def open_stream(req):
    """The route. The gate runs BEFORE a frame is written, and again on every tick."""
    row = service.load_visible(req.conn, conversation_id=int(req.params["id"]),
                               user=req.session.user)
    if row is None:
        from agento.web.api import error
        return error(404, "not found")
    # The header WINS whenever the client sent one, even when it is garbled: a resume the
    # browser mangled is "no cursor" (live), never a silent fall-through to `?after`, which
    # on `?after=0` would replay the whole thread to a client that only meant to resume.
    header = req.headers.get("Last-Event-ID") if req.headers else None
    # PRESENT, not non-empty: a blank header is one the client sent, and "sent something
    # unusable" is "no cursor" (live), the same as a garbled one. Reading blank as absent let
    # `?after=0` take over and replay the whole thread to a client that meant to resume.
    cursor = parse_cursor(header if header is not None else req.query.get("after"))
    return sse(frames(req.conn, conversation_id=row["id"],
                      session_token=req.session_token, cursor=cursor,
                      user_id=req.session.user.id))
