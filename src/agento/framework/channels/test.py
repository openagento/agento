from __future__ import annotations

from .base import PromptFragments


class TestChannel:
    """Lightweight channel for e2e tests — no Jira/toolbox dependency."""

    @property
    def name(self) -> str:
        return "blank"

    def get_prompt_fragments(self, reference_id: str, config: object | None = None) -> PromptFragments:
        return PromptFragments(
            read_context=f"This is e2e test {reference_id}.",
            respond="Respond with exactly one word: OK",
        )

    def get_followup_fragments(
        self, reference_id: str, instructions: str, config: object | None = None
    ) -> PromptFragments:
        return self.get_prompt_fragments(reference_id)
