"""The conversation channel (PRD E3-E5 §4.2).

A channel exists so a workflow can talk about *where* a task came from. This one has almost
nothing to say: the task arrived over the panel, the whole context is the thread itself, and
the workflow reads that from the database. So the fragments are short by design - a channel
that invented instructions here would be putting words in the user's turn.
"""
from __future__ import annotations

from agento.framework.channels.base import PromptFragments

_READ = "Kontekst to wątek rozmowy poniżej. Nie ma zewnętrznego systemu do odczytu."
_RESPOND = "Odpowiedz użytkownikowi w tym samym języku, w którym napisał."


class ConversationChannel:
    @property
    def name(self) -> str:
        return "conversation"

    def get_prompt_fragments(self, reference_id: str, config: object | None = None) -> PromptFragments:
        return PromptFragments(read_context=_READ, respond=_RESPOND)

    def get_followup_fragments(self, reference_id: str, instructions: str,
                               config: object | None = None) -> PromptFragments:
        return PromptFragments(read_context=_READ, respond=_RESPOND, extra=instructions)
