"""The subagent channel: a task delegated by another Agento job (agent_run).

There is no external system to read or answer: the prompt is the whole task, and the
final answer goes back to the parent job as `job.output`.
"""
from __future__ import annotations

from agento.framework.channels.base import PromptFragments

_READ = "The task below is the whole context. There is no external system to read."
_RESPOND = "Your final answer is returned to the parent job as your result."


class SubagentChannel:
    @property
    def name(self) -> str:
        return "subagent"

    def get_prompt_fragments(self, reference_id: str, config: object | None = None) -> PromptFragments:
        return PromptFragments(read_context=_READ, respond=_RESPOND)

    def get_followup_fragments(self, reference_id: str, instructions: str,
                               config: object | None = None) -> PromptFragments:
        return PromptFragments(read_context=_READ, respond=_RESPOND, extra=instructions)
