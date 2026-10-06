"""Conversations, their messages, and the §4.1 submission contract.

Two things carry the weight here:

* **One reach gate.** `load_visible` is the single place a read resolves a conversation,
  so §9's "losing access hides the thread from the next request" is true for every read
  route at once, and a scope the caller cannot reach is `None` — which every route renders
  as 404, never 403.
* **Two commits, never one.** A message is inserted `pending` and committed BEFORE the job
  is published, then advanced to `published` with the job id. A single transaction around
  an insert and an external publish would either announce a job that rolled back or lose a
  job nobody is waiting on. The cost is a `pending` row after a crash, which
  `complete_pending` finishes - idempotently, because the idempotency key is derived from
  the message id and not from the attempt.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymysql

from agento.framework.access.accounts import User, can_reach, scope_is_active
from agento.framework.events import (
    ConversationArchivedEvent,
    ConversationCreatedEvent,
    ConversationDeletedEvent,
    ConversationMessageEvent,
    ConversationReactivatedEvent,
    ConversationUnblockedEvent,
)
from agento.framework.job_models import JobRequester, RequesterTrust
from agento.framework.job_types import resolve_job_type
from agento.framework.publish_service import publish_job

MODULE_DIR = Path(__file__).resolve().parent.parent
JOB_TYPE = "conversation"
SOURCE = "conversation"
MAX_CLIENT_MESSAGE_ID = 128
MAX_TITLE = 255          # the `conversation.title` column


class SubmissionError(ValueError):
    """A refused submission. `status` is what the route answers."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def config(conn, path: str) -> int:
    """One `conversation/<path>` value, through the framework's own 3-level fallback."""
    from agento.framework.config_resolver import (
        load_db_overrides,
        read_config_defaults,
        resolve_field,
    )

    schema = json.loads((MODULE_DIR / "system.json").read_text())[path]
    return int(resolve_field("conversation", path, schema,
                             read_config_defaults(MODULE_DIR), load_db_overrides(conn)).value)


# --- reads -----------------------------------------------------------------

_FROM = "FROM conversation c LEFT JOIN agent_view v ON v.id = c.agent_view_id "
_SELECT = "SELECT c.*, v.workspace_id " + _FROM


def _reachable(conn, row: dict, user: User, *, scopes: dict | None = None) -> bool:
    """Owner (or admin), reach over the row's scope, and that scope still active.

    A conversation whose `agent_view_id` is NULL lost its view to a delete (§3.2's
    `ON DELETE SET NULL`). There is no scope left to reach or to deactivate, so it stays
    readable to its owner - that is the delete case, and it is not the deactivation case.

    `scopes` is a per-call memo of the scope half of the gate: the answer depends on the
    agent_view alone, and a page of one user's threads is a handful of distinct views. It
    keeps this the ONE gate while a list costs a query per view instead of per row (CODE-8).
    """
    if row["user_id"] != user.id and user.role != "admin":
        return False
    if row["agent_view_id"] is None:
        return True
    if scopes is not None and row["agent_view_id"] in scopes:
        return scopes[row["agent_view_id"]]
    allowed = (
        can_reach(conn, user, workspace_id=row["workspace_id"], agent_view_id=row["agent_view_id"])
        and scope_is_active(conn, row["agent_view_id"])
    )
    if scopes is not None:
        scopes[row["agent_view_id"]] = allowed
    return allowed


def load_visible(conn, *, conversation_id: int, user: User) -> dict | None:
    """The conversation this caller may read right now, or None."""
    with conn.cursor() as cur:
        cur.execute(_SELECT + "WHERE c.id = %s", (conversation_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return row if _reachable(conn, row, user) else None


_LIVE = ("EXISTS (SELECT 1 FROM execution e WHERE e.conversation_id = c.id "
         "AND e.status = 'running') AS live ")


def channel_cursor(row: dict) -> str:
    """The keyset position of a channel-list row: pass it back as `before` for the next page."""
    return f"{row['activity_ts']}:{row['id']}"


def list_visible(conn, *, user: User, limit: int, channels: bool = False,
                 channel: str | None = None, before: tuple[int, int] | None = None) -> list[dict]:
    """The caller's conversations, newest first, filtered by the same gate.

    `channels` lists the channel threads (`user_id IS NULL`) instead, newest activity
    first. They are admin-only (E9 §3.6): `_reachable` already refuses a row with no owner
    to a non-admin, and a non-admin is answered an empty list before any query. `before`
    is the `(activity_ts, id)` of the last row of the previous page (keyset, E9 §3.7).

    ponytail: the reach check is per row (up to `history/page_size` rows), not folded into
    the SQL, so there is one gate and not a second copy of it in a WHERE clause. The scope
    half of it is memoized per agent_view for the call, so the page costs one pair of reach
    queries per distinct view and not per row.
    """
    select = "SELECT c.*, v.workspace_id, " + _LIVE + _FROM
    if channels:
        if user.role != "admin":
            return []
        select = ("SELECT c.*, v.workspace_id, UNIX_TIMESTAMP(COALESCE(c.last_activity_at, "
                  "c.created_at)) AS activity_ts, " + _LIVE + _FROM)
        where, params = "WHERE c.user_id IS NULL AND c.status = 'active' ", []
        if channel:
            where += "AND c.channel = %s "
            params.append(channel)
        if before is not None:
            where += ("AND (COALESCE(c.last_activity_at, c.created_at), c.id) "
                      "< (FROM_UNIXTIME(%s), %s) ")
            params.extend(before)
        order = "ORDER BY COALESCE(c.last_activity_at, c.created_at) DESC, c.id DESC "
    else:
        where, params = "WHERE c.user_id = %s AND c.status = 'active' ", [user.id]
        order = "ORDER BY c.updated_at DESC, c.id DESC "
    with conn.cursor() as cur:
        cur.execute(select + where + order + "LIMIT %s", (*params, limit))
        rows = list(cur.fetchall())
    scopes: dict[int, bool] = {}
    return [r for r in rows if _reachable(conn, r, user, scopes=scopes)]


MAX_RUNS = 50


def list_runs(conn, *, conversation_id: int) -> list[dict]:
    """The thread's runs, newest first, with what the job row knows about each."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.execution_id, e.job_id, e.attempt, e.status, e.started_at, "
            "       e.finished_at, j.type, j.agent_type, j.model, j.input_tokens, "
            "       j.output_tokens "
            "FROM execution e LEFT JOIN job j ON j.id = e.job_id "
            "WHERE e.conversation_id = %s ORDER BY e.id DESC LIMIT %s",
            (conversation_id, MAX_RUNS))
        return list(cur.fetchall())


def is_live(conn, *, conversation_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM execution WHERE conversation_id = %s "
                    "AND status = 'running' LIMIT 1", (conversation_id,))
        return cur.fetchone() is not None


def list_messages(conn, *, conversation_id: int, limit: int, after_id: int = 0) -> list[dict]:
    """The thread's messages, each carrying whether its turn is blocked and why.

    The join is LEFT: an assistant row has no job, and a user row outlives its job (jobs are
    pruned on their own schedule). A reader must be able to tell "waiting" from "stuck",
    which is the whole point of §4.5 - a thread only a database edit could free would be
    indistinguishable from a slow agent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.role, m.content, m.client_message_id, m.job_id, m.job_state, "
            "       m.created_at, j.status AS job_status, j.session_id "
            "FROM message m LEFT JOIN job j ON j.id = m.job_id "
            "WHERE m.conversation_id = %s AND m.id > %s ORDER BY m.id LIMIT %s",
            (conversation_id, after_id, limit),
        )
        return [dict(row, blocked_reason=blocked_reason(row)) for row in cur.fetchall()]


# One page of replay can never be larger than this, whatever `history/page_size` says.
# The cursor is the contract: a client that asks for everything gets a page and the id to
# come back with, so an operator raising the page size cannot turn one request into a
# whole-thread read of a table that grows with every delta (§6.4).
MAX_EVENT_PAGE = 500


def list_events(conn, *, conversation_id: int, after_id: int, limit: int) -> list[dict]:
    """The thread's events after `after_id`, oldest first - the replay half of §6.4.

    `conversation_event.id` is the stream cursor and it is global, not per thread, so a
    caller replays with the id it last saw and never with a count.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, payload, execution_id, created_at FROM conversation_event "
            "WHERE conversation_id = %s AND id > %s ORDER BY id LIMIT %s",
            (conversation_id, after_id, min(limit, MAX_EVENT_PAGE)),
        )
        return list(cur.fetchall())


def list_timeline(conn, *, conversation_id: int, before_id: int | None,
                  limit: int) -> tuple[list[dict], bool]:
    """The newest page of events (or the page before `before_id`), oldest first, and
    whether older events exist. Keyset on the global id: a page never overlaps the next."""
    limit = max(1, min(limit, MAX_EVENT_PAGE))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, payload, execution_id, created_at FROM conversation_event "
            "WHERE conversation_id = %s AND id < %s ORDER BY id DESC LIMIT %s",
            (conversation_id, before_id if before_id is not None else 2**63, limit + 1))
        rows = list(cur.fetchall())
    return rows[:limit][::-1], len(rows) > limit


# --- the client shape of an event (E9 §3.6) ----------------------------------

# Tool payloads can carry customer data from tools a panel user holds no grant on, so they
# are an admin's only. The name, the call id and the error flag stay for everyone.
_ADMIN_ONLY_DATA = ("input", "output")
_PROMPTED = ("run.started", "run.finished")


def project_events(conn, rows: list[dict], user: User) -> list[dict]:
    """The ONE client shape of an event, for the timeline, the replay and the stream.

    Batch reads, one query per page each (CODE-8): the text of `message.created`, and the
    trigger prompt of `run.started` and `run.finished` (admins only). The consumer writes
    `job.prompt` in its terminal update, the transaction that also writes `run.finished`,
    so a client that already holds `run.started` reads the prompt from `run.finished`.
    """
    def payload_of(row: dict) -> dict:
        payload = row["payload"]
        return dict(json.loads(payload) if isinstance(payload, str) else payload or {})

    payloads = [payload_of(r) for r in rows]
    message_ids = {p["message_id"] for r, p in zip(rows, payloads, strict=True)
                   if r["kind"] == "message.created" and p.get("message_id")}
    contents: dict[int, str] = {}
    if message_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT id, content FROM message WHERE id IN "
                        f"({','.join(['%s'] * len(message_ids))})", list(message_ids))
            contents = {m["id"]: m["content"] for m in cur.fetchall()}
    admin = user.role == "admin"
    job_ids = {p["job_id"] for r, p in zip(rows, payloads, strict=True)
               if admin and r["kind"] in _PROMPTED and p.get("job_id")}
    prompts: dict[int, str] = {}
    if job_ids:
        from .finalizer import truncate_utf8

        cap = config(conn, "limits/max_message_bytes")
        with conn.cursor() as cur:
            cur.execute("SELECT id, prompt FROM job WHERE id IN "
                        f"({','.join(['%s'] * len(job_ids))})", list(job_ids))
            prompts = {j["id"]: truncate_utf8(j["prompt"], cap)
                       for j in cur.fetchall() if j["prompt"]}

    out = []
    for row, payload in zip(rows, payloads, strict=True):
        if row["kind"] == "message.created" and payload.get("message_id") in contents:
            payload["content"] = contents[payload["message_id"]]
        if row["kind"] in _PROMPTED and payload.get("job_id") in prompts:
            payload["prompt"] = prompts[payload["job_id"]]
        if not admin and isinstance(payload.get("data"), dict):
            payload["data"] = {k: v for k, v in payload["data"].items()
                               if k not in _ADMIN_ONLY_DATA}
        created = row.get("created_at")
        out.append({"id": row["id"], "kind": row["kind"], "execution_id": row["execution_id"],
                    "payload": payload,
                    "created_at": None if created is None else created.isoformat() + "Z"})
    return out


def blocked_reason(message: dict) -> str | None:
    """Why this turn cannot finish on its own, or None.

    `paused_unrecoverable` is §5.3's trap: `resume_job()` refuses a paused job with no
    session for ever, and §4.4 then defers every later turn of the thread. `paused` is the
    ordinary case, which an operator can resume.
    """
    if message.get("job_state") != "published" or message.get("job_status") != "PAUSED":
        return None
    return "paused" if message.get("session_id") else "paused_unrecoverable"


# --- writes ----------------------------------------------------------------

def _dispatch(name: str, event: object) -> None:
    """Announce a committed state change. Every one of these is dispatched AFTER its
    commit: an observer is a fail-open reaction (F22) and must never sit inside the
    transaction whose durability the thread depends on."""
    from agento.framework.event_manager import get_event_manager

    get_event_manager().dispatch(name, event)


def create_conversation(conn, *, user_id: int, agent_view_id: int | None, title: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation (user_id, agent_view_id, title) VALUES (%s, %s, %s)",
            (user_id, agent_view_id, title),
        )
        conversation_id = cur.lastrowid
    conn.commit()
    _dispatch("conversation_create_after", ConversationCreatedEvent(
        conversation_id=conversation_id, agent_view_id=agent_view_id, user_id=user_id))
    return conversation_id


def archive(conn, conversation_id: int, *, actor_id: int | None = None,
            reason: str = "manual") -> bool:
    """The ONE archive path. §10.1 retires an idle thread through this same function, so
    there is one event and not two that can drift (`reason` is what tells them apart).

    Returns True when it changed something. The UPDATE decides, not the caller: an event
    is an announcement that a transition HAPPENED, so a second archive of an already
    archived thread - or of one that is gone - announces nothing.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET status = 'archived' WHERE id = %s "
                    "AND status <> 'archived'", (conversation_id,))
        changed = cur.rowcount == 1
    conn.commit()
    if changed:
        _dispatch("conversation_archive_after", ConversationArchivedEvent(
            conversation_id=conversation_id, actor_id=actor_id, reason=reason))
    return changed


def _reactivate_locked(cur, conversation_id: int) -> bool:
    """Revive the thread under the conversation row lock. True when it changed something.

    The lock is the whole point and is held until the CALLER commits: §10.1's archive and
    delete take the same one, so a post and a retention pass serialize instead of racing.
    A caller that commits before inserting its message would hand the archive pass the gap
    between the two, and the post would land in a thread that is archived again.
    """
    cur.execute("SELECT status FROM conversation WHERE id = %s FOR UPDATE",
                (conversation_id,))
    row = cur.fetchone()
    if row is None:
        raise SubmissionError(404, "not found")
    if row["status"] == "active":
        return False
    cur.execute("UPDATE conversation SET status = 'active' WHERE id = %s", (conversation_id,))
    return True


def reactivate(conn, conversation_id: int, *, actor_id: int) -> bool:
    """The ONE reactivation path: the operator's, and §3.2's post into an archived thread.

    The post takes the same locked mutation inside its OWN transaction (`submit_message`),
    so the thread and its first new message revive together. Returns True when it changed
    something - an already-active thread is a no-op, not a second
    `conversation_reactivate_after`.
    """
    with conn.cursor() as cur:
        try:
            changed = _reactivate_locked(cur, conversation_id)
        except SubmissionError:
            conn.commit()               # release the lock the SELECT took
            raise
    conn.commit()
    if changed:
        _dispatch("conversation_reactivate_after", ConversationReactivatedEvent(
            conversation_id=conversation_id, actor_id=actor_id))
    return changed


class UnblockRefused(SubmissionError):
    """An unblock the guard refused. `status` is what the route answers."""


def unblock(conn, *, conversation_id: int, message_id: int, actor_id: int) -> bool:
    """Free a thread stuck on an unrecoverable paused turn (§4.5). True if it changed.

    TWO conditions, both required, and neither is optional:

    1. the job is `PAUSED` with no session, or the message is already terminal. Marking a
       RESUMABLE turn terminal would let the next turn run beside it when the job resumes -
       exactly what §4.4 exists to prevent.
    2. the turn's newest `execution` is no longer `running`. `PAUSED` with no session also
       holds while a stopped process is still alive (a request that timed out, or a CLI
       pause whose short wait gave up), and freeing the turn there would start the next one
       beside that process. A non-`running` execution means an acknowledging path has
       already finalized it (§5.3).

    It does NOT touch the job: the `PAUSED` row stays for an operator to inspect.
    """
    # ONE transaction, and the rows are locked in it. Reading the message, the job and
    # the newest execution in three committed steps let a resume start between them: the
    # guards would pass against a job that is RUNNING again by the time the turn is marked
    # terminal, and §4.4 would then let the next turn run beside a live one. The `job` row
    # is the same one `_issue_run_capabilities` takes `FOR UPDATE` before it mints, so a
    # claim either lands before this read or waits for this commit.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.job_id, m.job_state, m.role, j.status AS job_status, "
            "       j.session_id "
            "FROM message m LEFT JOIN job j ON j.id = m.job_id "
            "WHERE m.id = %s AND m.conversation_id = %s FOR UPDATE",
            (message_id, conversation_id))
        message = cur.fetchone()

        refusal = None
        if message is None or message["role"] != "user":
            refusal = UnblockRefused(404, "message not found")
        elif message["job_state"] == "terminal":
            refusal = None                      # already free: a no-op, not an error
        elif message["job_status"] != "PAUSED" or message["session_id"] is not None:
            refusal = UnblockRefused(
                409, "the turn is not stuck: only a paused turn with no "
                     "session can be unblocked")
        else:
            cur.execute(
                "SELECT status FROM execution WHERE job_id = %s ORDER BY id DESC LIMIT 1",
                (message["job_id"],))
            execution = cur.fetchone()
            if execution is not None and execution["status"] == "running":
                refusal = UnblockRefused(
                    409, "the run has not been finalized yet; try again shortly")

        changed = False
        if refusal is None and message is not None and message["job_state"] != "terminal":
            cur.execute("UPDATE message SET job_state = 'terminal' WHERE id = %s AND "
                        "job_state = 'published'", (message_id,))
            # The UPDATE, not the read, decides: two concurrent unblocks both pass the
            # guards, and only the one whose UPDATE matched a row may say it changed
            # anything or dispatch the event.
            changed = cur.rowcount == 1
    conn.commit()
    if refusal is not None:
        raise refusal
    if changed:
        _dispatch("conversation_unblock_after", ConversationUnblockedEvent(
            conversation_id=conversation_id, message_id=message_id, actor_id=actor_id))
    return changed


def delete_conversation(conn, conversation_id: int) -> bool:
    """Delete a thread and everything under it (§10.1's operator path).

    The event is notification only and is dispatched after the commit: by then the row is
    gone, so there is nothing for an observer to read and nothing it can change.
    """
    from .retention import delete_tree

    # The SAME ordered delete the retention pass runs: an operator path that relied on the
    # DB cascade would drop `execution` rows nothing cascades to (they hang off `job`).
    if not delete_tree(conn, conversation_id):
        return False                # there was no thread; nothing was deleted to announce
    _dispatch("conversation_delete_after",
              ConversationDeletedEvent(conversation_id=conversation_id))
    return True


def lock_conversations(cur, conversation_ids) -> None:
    """Take the threads' row locks, in ascending id - the lock-order invariant (E9 §3.4) -
    and move their activity clock. One UPDATE does both: it locks the primary-key rows in
    index order, and every caller is about to write the threads' timeline."""
    ids = sorted(set(conversation_ids))
    if ids:
        cur.execute("UPDATE conversation SET last_activity_at = NOW() "
                    f"WHERE id IN ({','.join(['%s'] * len(ids))})", ids)


def append_event(cur, conversation_id: int, *, kind: str, payload: dict, source_kind: str,
                 source_id: int, execution_id: str | None = None,
                 locked: bool = False) -> int | None:
    """The ONE writer of `conversation_event`. Returns the new id, or None for a repeat.

    The cursor is a global AUTO_INCREMENT, so two writers of one thread could take ids 10
    and 11 and commit 11 first - and a reader polling `id > cursor` would then move past 10
    for ever. The thread's row lock, taken before the insert and held to the commit, makes
    ids of one thread commit in the order they were allocated. A caller that writes several
    threads locks them all first with `lock_conversations` and passes `locked=True`.

    INSERT IGNORE: `uq_source` is the idempotency, and a re-delivery costs nothing.
    """
    if not locked:
        lock_conversations(cur, [conversation_id])
    cur.execute(
        "INSERT IGNORE INTO conversation_event "
        "(conversation_id, execution_id, kind, payload, source_kind, source_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (conversation_id, execution_id, kind, json.dumps(payload), source_kind, source_id),
    )
    return cur.lastrowid if cur.rowcount == 1 else None


# --- channel threads (E9 §3.5) ------------------------------------------------

def channel_key(source: str, agent_view_id: int | None, reference_id: str | None,
                job_id: int) -> str:
    """The dedupe key of a channel thread: one per (source, view, reference)."""
    ref = reference_id or f"job:{job_id}"
    return hashlib.sha1(f"{source}|{agent_view_id or ''}|{ref}".encode()).hexdigest()


def link_execution(cur, *, execution_row_id: int, execution_id: str, job_id: int,
                   attempt: int) -> int | None:
    """Give a freshly minted run its thread, and announce it there. Returns the thread.

    A panel job (`source = 'conversation'`) is in the thread of the message it answers.
    Every other job is in its **channel thread**, keyed by `(source, view, reference)` and
    created or reactivated here - so the next run on the same Jira issue, and a follow-up
    that copies its parent's source and reference, land in the same thread.
    """
    cur.execute("SELECT type, source, reference_id, agent_view_id FROM job WHERE id = %s",
                (job_id,))
    job = cur.fetchone()
    if job is None:
        return None
    if job["source"] == SOURCE:
        from .workflow import ReferenceUnusable, parse_reference
        try:
            _, message_id = parse_reference(job["reference_id"])
        except ReferenceUnusable:
            return None
        cur.execute("SELECT conversation_id FROM message WHERE id = %s", (message_id,))
        row = cur.fetchone()
        if row is None:
            return None
        conversation_id = row["conversation_id"]
    else:
        ref = job["reference_id"]
        title = f"{job['source']} {ref or f'job {job_id}'}"[:MAX_TITLE]
        cur.execute(
            # The view through a sub-select: a job may outlive its view, and the FK would
            # then refuse the insert and with it the claim.
            "INSERT INTO conversation (user_id, agent_view_id, title, channel, external_ref, "
            "                          external_key, last_activity_at) "
            "VALUES (NULL, (SELECT id FROM agent_view WHERE id = %s), %s, %s, %s, %s, NOW()) "
            "ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id), status = 'active'",
            (job["agent_view_id"], title, job["source"], ref,
             channel_key(job["source"], job["agent_view_id"], ref, job_id)))
        conversation_id = cur.lastrowid
    cur.execute("UPDATE execution SET conversation_id = %s WHERE id = %s",
                (conversation_id, execution_row_id))
    append_event(cur, conversation_id, kind="run.started", execution_id=execution_id,
                 source_kind="execution", source_id=execution_row_id,
                 payload={"job_id": job_id, "attempt": attempt, "type": job["type"],
                          "source": job["source"], "reference_id": job["reference_id"]})
    return conversation_id


def _utf8(value: str, what: str) -> bytes:
    """The text as the bytes everything downstream stores and sends.

    JSON can carry a lone surrogate (`"\\ud800"`); UTF-8 cannot encode one. Refused here it
    is a 400 the caller can act on; carried past this gate, the first encode - the column,
    the SSE frame, a log line - raises `UnicodeEncodeError` and the caller gets a 500 for
    an input that was theirs to fix. Every text input this module accepts goes through it.
    """
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise SubmissionError(400, f"{what} must be valid UTF-8") from None


def check_client_message_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CLIENT_MESSAGE_ID:
        raise SubmissionError(
            400, f"client_message_id must be 1..{MAX_CLIENT_MESSAGE_ID} characters")
    _utf8(value, "client_message_id")
    return value


def check_content(conn, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SubmissionError(400, "content must be a non-empty string")
    # Bytes, not characters: the column and every relay downstream count bytes.
    if len(_utf8(value, "content")) > config(conn, "limits/max_message_bytes"):
        raise SubmissionError(413, "message too large")
    return value


def check_title(value: object) -> str | None:
    """A thread may have no title; a title it has must be storable."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TITLE:
        raise SubmissionError(400, f"title must be a string of at most {MAX_TITLE} characters")
    _utf8(value, "title")
    return value


def submit_message(conn, *, conversation_id: int, user_id: int, client_message_id: str,
                   content: str, reactivate_actor_id: int | None = None
                   ) -> tuple[int, int, bool]:
    """(message_id, job_id, created). §4.1's three steps, of which this is 1 and 3.

    `reactivate_actor_id` makes the post into an archived thread (§3.2) part of THIS
    transaction: the revival and the pending message commit together, so the retention
    pass cannot archive the thread in between and leave the new turn in an archived one.
    """
    created = True
    revived = False
    try:
        with conn.cursor() as cur:
            # The thread's row lock BEFORE the message insert (E9 §3.4). The insert's FK
            # check takes a shared lock on the same row, so two posts that each hold one
            # and then ask `append_event` for the exclusive lock would deadlock.
            if reactivate_actor_id is not None:
                revived = _reactivate_locked(cur, conversation_id)
            else:
                lock_conversations(cur, [conversation_id])
            cur.execute(
                "INSERT INTO message (conversation_id, role, content, client_message_id, job_state) "
                "VALUES (%s, 'user', %s, %s, 'pending')",
                (conversation_id, content, client_message_id),
            )
            message_id = cur.lastrowid
            # Only the winner announces the turn: the re-read path below is a replay of a
            # turn the thread has already announced, and the event row has no uniqueness
            # of its own that would save it from a second announcement.
            append_event(cur, conversation_id, kind="message.created",
                         payload={"message_id": message_id, "role": "user"},
                         source_kind="message", source_id=message_id,
                         locked=reactivate_actor_id is None)
        conn.commit()
    except pymysql.err.IntegrityError:
        conn.rollback()                 # the revival rolls back with it
        created = False
        revived = False
        if reactivate_actor_id is not None:
            # A replay of a turn this thread already holds. The thread still has to end up
            # active, and `reactivate` commits and announces on its own.
            reactivate(conn, conversation_id, actor_id=reactivate_actor_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM message WHERE conversation_id = %s AND client_message_id = %s",
                (conversation_id, client_message_id),
            )
            row = cur.fetchone()
        if row is None:
            raise
        message_id = row["id"]

    if revived:
        _dispatch("conversation_reactivate_after", ConversationReactivatedEvent(
            conversation_id=conversation_id, actor_id=reactivate_actor_id))
    # The event belongs to `complete_pending`: it is the call that publishes, and it is
    # reached from the sweep too. Announced from here it also fired for a REPLAY, which
    # accepts nothing and publishes nothing - an event for a transition that did not happen.
    job_id = complete_pending(conn, message_id)
    return message_id, job_id, created


def complete_pending(conn, message_id: int) -> int:
    """Publish the job for a `pending` message and record it. Idempotent (§4.1 steps 2-3).

    The idempotency key is derived from the message id, so a crash between the publish and
    the update re-publishes into the same job instead of a second one.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.content, m.job_id, m.job_state, m.conversation_id, "
            "       c.agent_view_id, c.user_id "
            "FROM message m JOIN conversation c ON c.id = m.conversation_id "
            "WHERE m.id = %s",
            (message_id,),
        )
        row = cur.fetchone()
    conn.commit()  # the SELECT opened a transaction
    if row is None:
        raise SubmissionError(404, "message not found")
    if row["job_state"] != "pending":
        return row["job_id"]              # already published: nothing happened here

    job_id = publish_job(
        source=SOURCE,
        agent_type=resolve_job_type(JOB_TYPE),
        agent_view_id=row["agent_view_id"],
        reference_id=f"{row['conversation_id']}:{row['id']}",
        idempotency_key=f"conversation:{row['conversation_id']}:{row['id']}",
        requester=JobRequester(key=f"user:{row['user_id']}", trust=RequesterTrust.ACCOUNT),
        priority=50,
        prompt=row["content"],
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE message SET job_id = %s, job_state = 'published' "
            "WHERE id = %s AND job_state = 'pending'",
            (job_id, message_id),
        )
        # Two callers can both read `pending` - the route and the sweep, or two sweeps. The
        # conditional UPDATE is what decides which one made the transition; ignoring its
        # rowcount announced the same turn twice (EVT-4). The loser answers with the job id
        # that was stored, which the idempotency key makes the same job anyway.
        published_here = cur.rowcount == 1
        if not published_here:
            cur.execute("SELECT job_id FROM message WHERE id = %s", (message_id,))
            job_id = (cur.fetchone() or {}).get("job_id") or job_id
    conn.commit()
    if published_here:
        _dispatch("conversation_message_after", ConversationMessageEvent(
            conversation_id=row["conversation_id"], message_id=message_id, job_id=job_id))
    return job_id


def sweep_pending(conn, *, grace_seconds: int, limit: int = 500) -> int:
    """Finish every `pending` message older than the grace period. Returns how many."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM message WHERE job_state = 'pending' "
            # <=, so a zero grace period means "every pending row", not "none of the ones
            # written this second".
            "AND created_at <= NOW() - INTERVAL %s SECOND ORDER BY id LIMIT %s",
            (grace_seconds, limit),
        )
        ids = [r["id"] for r in cur.fetchall()]
    conn.commit()
    for message_id in ids:
        complete_pending(conn, message_id)
    return len(ids)


def reconcile_terminal(conn, limit: int = 500) -> int:
    """Advance `published` messages whose job already finished (PRD:172).

    Normally §5.3's finalizer does this. A crash between the job's terminal commit and that
    write would strand a message at `published` for ever, and §4.4 reads exactly this column
    to decide whether the next turn may run - so the thread would be blocked with no way out.

    `TODO`, `RUNNING` and `PAUSED` are left alone on purpose: a retry and a stale recovery
    both pass back through `TODO` with the execution finished, and advancing there would let
    the next turn run beside the retry. `PAUSED` is §4.5's case and has its own route.
    """
    with conn.cursor() as cur:
        # Selected first, then updated by id: MySQL refuses LIMIT on a multi-table UPDATE.
        cur.execute(
            "SELECT m.id FROM message m JOIN job j ON j.id = m.job_id "
            "WHERE m.job_state = 'published' AND j.status IN ('SUCCESS','FAILED','DEAD') "
            "ORDER BY m.id LIMIT %s",
            (limit,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            conn.commit()
            return 0
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            f"UPDATE message SET job_state = 'terminal' "
            f"WHERE job_state = 'published' AND id IN ({placeholders})",
            tuple(ids),
        )
        advanced = cur.rowcount
    conn.commit()
    return advanced
