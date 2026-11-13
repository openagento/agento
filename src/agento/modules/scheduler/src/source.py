"""The ``scheduler`` schedule source (AG-67 plan §5.4): what one fire of an editable schedule
publishes. The framework tick and Run now both call it through ``tick.fire``."""
from __future__ import annotations

from agento.framework.agent_view_runtime import resolve_agent_view_context
from agento.framework.job_models import JobRequester, RequesterTrust
from agento.framework.schedule.sources import FireSpec
from agento.modules.conversation.src import service as conversation

from .rights import SOURCE

DELETED_SUFFIX = " (schedule deleted)"


def thread_ref(schedule_id: int) -> str:
    return f"schedule:{schedule_id}"


def header(row: dict) -> str:
    return f'Scheduled task "{row["title"]}" (schedule #{row["id"]}).'


def thread_id(cur, row: dict) -> int | None:
    """The schedule's thread in its target view, if a run made it. A plain read."""
    if row["agent_view_id"] is None:
        return None
    cur.execute("SELECT id FROM thread WHERE agent_view_id = %s AND ref_hash = SHA1(%s)",
                (row["agent_view_id"], thread_ref(row["id"])))
    found = cur.fetchone()
    return found["id"] if found else None


class SchedulerSource:
    @property
    def name(self) -> str:
        return SOURCE

    @property
    def editable(self) -> bool:
        return True

    def fire_spec(self, cur, row: dict) -> FireSpec:
        # A user owner runs as that user (D-10, D-33); an agent owner has no account identity.
        requester = (JobRequester(key=f"user:{row['owner_user_id']}", trust=RequesterTrust.ACCOUNT)
                     if row["owner_user_id"] is not None else None)
        priority = resolve_agent_view_context(cur.connection, row["agent_view_id"]).priority
        return FireSpec(SOURCE, SOURCE, str(row["id"]), thread_ref(row["id"]), requester,
                        f"{header(row)}\n\n{row['prompt'] or ''}", priority)

    def on_delete(self, cur, row: dict) -> None:
        """Mark the conversation, in the deleting transaction (``tick.finalize_delete``). The
        audit row is written where ``deleted_at`` is set, which knows the actor."""
        tid = thread_id(cur, row)
        if tid is None:
            return
        cur.execute("SELECT title FROM conversation WHERE thread_id = %s", (tid,))
        found = cur.fetchone()
        if found is not None and not found["title"].endswith(DELETED_SUFFIX):
            conversation.retitle(cur, tid, found["title"] + DELETED_SUFFIX)
