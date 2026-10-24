"""One mail conversation = one thread, only while thread read is on (SEC-7).

A thread resumes its earlier turns' session. The earlier messages of a mail conversation are
readable only through `outlook_list_thread`, so a message joins its conversation's thread only
when that tool is effective for the view; else it keeps its own per-message thread, as before.
The tool stays the single gate (CLAUDE.md "Routing"): there is no second switch.

The gate is checked again at every claim (`OutlookThreadGate`), because the tool can be turned
off while jobs of a conversation thread wait. Then the claim detaches the job: it runs in its
own default thread with no resumed session.
"""
from __future__ import annotations

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection, pooled
from agento.framework.events import JobClaimBeforeEvent
from agento.framework.tool_enablement import is_effective_for_view

THREAD_READ_TOOL = "outlook_list_thread"
# Its own prefix: a job's default ref is `outlook:<reference_id>` (threads.default_ref), and
# that per-message thread holds only the job's own trigger, which needs no gate.
CONVERSATION_REF_PREFIX = "outlook:conversation:"


def conversation_ref(conversation_id: str) -> str:
    """The thread ref of one Graph mail conversation."""
    return f"{CONVERSATION_REF_PREFIX}{conversation_id}"


def thread_read_on(conn, agent_view_id: int) -> bool:
    """Whether `outlook_list_thread` is effective for the view (its own value and its chain)."""
    return is_effective_for_view(conn, THREAD_READ_TOOL, agent_view_id=agent_view_id)


class OutlookThreadGate:
    """Detach a job from a mail-conversation thread whose view has thread read off.

    Keyed on the THREAD, not on the job's source: a queued message, a follow-up, a wake or a
    delegated child in another view all join the thread the same way, so all are checked. The
    view is the thread's own (the mailbox's view). A thread whose view was deleted has no
    owner, so it detaches too.
    """

    def execute(self, event: object) -> None:
        if not isinstance(event, JobClaimBeforeEvent):
            return
        # A pooled connection that only reads; the claim transaction (which holds the job row)
        # is open on another one.
        with pooled(DatabaseConfig.from_env(), get_connection) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT t.ref, t.agent_view_id FROM job j JOIN thread t ON t.id = j.thread_id "
                    "WHERE j.id = %s", (event.job_id,))
                thread = cur.fetchone()
            if thread is None or not thread["ref"].startswith(CONVERSATION_REF_PREFIX):
                return
            view = thread["agent_view_id"]
            if view is None or not thread_read_on(conn, view):
                event.detach_thread = True
