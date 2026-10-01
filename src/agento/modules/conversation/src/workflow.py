"""The conversation workflow (PRD E3-E5 §4.2).

`job.reference_id` is `"<conversation id>:<message id>"`, written by `publish_job` in the
insert itself, so the row a claim reads is never half-written.

**The prompt ends at that message id.** Not "every message of the thread": while turn 1
waits to be claimed the user may already have queued turn 2, and §4.4 defers turn 2's *job*
but does nothing to stop turn 1's *prompt* from swallowing it. The thread would then answer
a question the user was never told had arrived, and turn 2's own job would ask it again.
"""
from __future__ import annotations

from agento.framework.channels.base import Channel
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.harness import RunRequest, RunResult
from agento.framework.job_models import Job
from agento.framework.workflows.base import JobContext, Workflow

_ROLE_LABEL = {"user": "Użytkownik", "assistant": "Asystent"}


class ReferenceUnusable(ValueError):
    """An unusable `reference_id`. Shaped like `get_channel()`'s error: it names what it
    got and what it expected, because it is read in a log, not in a debugger."""


def parse_reference(reference_id: str | None) -> tuple[int, int]:
    conversation_id, _, message_id = (reference_id or "").partition(":")
    if not conversation_id.isdigit() or not message_id.isdigit():
        raise ReferenceUnusable(
            f"Unusable conversation reference_id: {reference_id!r}. "
            "Expected '<conversation id>:<message id>'."
        )
    return int(conversation_id), int(message_id)


def load_turns(conn, conversation_id: int, up_to_message_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, content FROM message "
            "WHERE conversation_id = %s AND id <= %s ORDER BY id",
            (conversation_id, up_to_message_id),
        )
        return list(cur.fetchall())


class ConversationWorkflow(Workflow):
    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        """One turn. A resumed session already holds the thread, so it is sent the new turn
        alone (§5.2); a fresh one is sent the assembled history as well."""
        reference_id = job.reference_id or ""
        session_id = context.resume_session_id
        prompt = self.build_prompt(channel, reference_id, history=session_id is None)
        result = self.runner.execute(RunRequest(prompt=prompt, session_id=session_id))
        result.prompt = prompt
        self.logger.info(
            f"channel={channel.name} ref={reference_id} "
            f"resumed={session_id is not None} {result.stats_line}"
        )
        return result

    def build_prompt(self, channel: Channel, reference_id: str, **kwargs: object) -> str:
        conversation_id, message_id = parse_reference(reference_id)
        conn = kwargs.get("conn") or get_connection(DatabaseConfig.from_env())
        try:
            turns = load_turns(conn, conversation_id, message_id)
        finally:
            if kwargs.get("conn") is None:
                conn.close()
        if not turns:
            raise ReferenceUnusable(
                f"No message {message_id} in conversation {conversation_id} "
                f"(reference_id {reference_id!r})."
            )

        fragments = channel.get_prompt_fragments(reference_id)
        # The bound is the same either way: a resumed turn must not pick up a turn queued
        # behind it any more than a fresh one may.
        if kwargs.get("history", True) is False:
            turns = turns[-1:]
        history = "\n\n".join(
            f"{_ROLE_LABEL.get(t['role'], t['role'])}: {t['content']}" for t in turns
        )
        return (
            f"{fragments.read_context}\n\n"
            f"=== WĄTEK ===\n{history}\n=== KONIEC WĄTKU ===\n\n"
            f"{fragments.respond}"
        )
