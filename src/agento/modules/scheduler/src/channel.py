"""The scheduler channel: a scheduled task, and the conversation it runs in (AG-67 plan §5.5,
§8.3). The conversation seam members only read, with plain reads, and never commit, except
``conversation_lock``, which locks the schedule row (docs/architecture/conversation-access.md)."""
from __future__ import annotations

import re

from agento.framework.channels.base import PromptFragments
from agento.framework.schedule import tick

from . import rights
from .source import thread_ref

_READ = "The task below is the whole context. There is no external system to read."
_RESPOND = "Your final answer is shown in the schedule's conversation in the panel."


def _id(reference_id: str | None) -> int | None:
    if not reference_id or not re.fullmatch(r"[0-9]+", reference_id):  # ASCII: isdigit() takes "²"
        return None
    return int(reference_id)


def _row(cur, reference_id: str | None) -> dict | None:
    if (schedule_id := _id(reference_id)) is None:
        return None
    cur.execute("SELECT * FROM schedule WHERE id = %s", (schedule_id,))
    return cur.fetchone()


class SchedulerChannel:
    @property
    def name(self) -> str:
        return rights.SOURCE

    def get_prompt_fragments(self, reference_id: str, config: object | None = None) -> PromptFragments:
        return PromptFragments(read_context=_READ, respond=_RESPOND)

    def get_followup_fragments(self, reference_id: str, instructions: str,
                               config: object | None = None) -> PromptFragments:
        # A reply posted in the schedule's conversation (D-29).
        return PromptFragments(read_context=_READ, respond=_RESPOND, extra=instructions)

    def conversation_owner(self, cur, reference_id: str) -> tuple[int | None, str] | None:
        row = _row(cur, reference_id)
        if row is None or row["deleted_at"] is not None:
            return None
        return row["owner_user_id"], row["title"]

    def conversation_access(self, conn, user, conversation: dict, mode: str) -> bool | None:
        with conn.cursor() as cur:
            row = _row(cur, conversation.get("external_ref"))
        if row is None:
            return None  # not a schedule, or gone: the existing gate applies
        if mode == "delete":
            return False  # the conversation stays while its schedule exists (D-32)
        if user is None:
            return None
        held = rights.rights(conn, user, row)
        return held.write if mode == "write" else held.read

    def conversation_lock(self, cur, conversation: dict) -> None:
        # The write comes from this row (owner) and its members (share). Every write that
        # changes them (store.share, store.change_owner, delete, the tools) locks it first, so
        # a post and a revoke serialize: the post sees the revoke, or the revoke waits (SEC-7).
        if (schedule_id := _id(conversation.get("external_ref"))) is not None:
            tick.lock_row(cur, schedule_id)

    def shared_thread_ids(self, conn, user, *, before: int | None, limit: int) -> list[int]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id FROM schedule_member m"
                " JOIN schedule s ON s.id = m.schedule_id AND s.deleted_at IS NULL"
                " JOIN thread t ON t.agent_view_id = s.agent_view_id"
                "  AND t.ref_hash = SHA1(CONCAT(%s, s.id))"
                " WHERE m.user_id = %s AND (%s IS NULL OR t.id < %s) ORDER BY t.id DESC LIMIT %s",
                (thread_ref(""), user.id, before, before, limit))
            return [r["id"] for r in cur.fetchall()]
