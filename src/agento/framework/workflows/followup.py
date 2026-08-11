from __future__ import annotations

from ..channels.base import Channel
from ..harness import RunResult
from ..job_models import Job
from .base import JobContext, Workflow


class FollowupWorkflow(Workflow):
    """Scheduled continuation of a previous task."""

    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        if not job.reference_id:
            raise ValueError(f"Followup job {job.id} has no reference_id")
        if not job.context:
            raise ValueError(f"Followup job {job.id} has no context")
        return self.execute(channel, job.reference_id, instructions=job.context)

    def build_prompt(
        self, channel: Channel, reference_id: str, **kwargs: object
    ) -> str:
        instructions = kwargs["instructions"]
        f = channel.get_followup_fragments(reference_id, str(instructions))

        intro = f.followup_intro or f"Kontynuacja zadania ({channel.name}) {reference_id}."
        lines = [
            f"{intro} To jest zaplanowany follow-up.",
            "",
        ]

        if f.extra:
            lines.append(f.extra)
            lines.append("")

        lines.append("Postępuj krok po kroku:")
        lines.append("")
        lines.append(f"1. {f.read_context}")
        lines.append(
            "2. Wykonaj instrukcje z sekcji KONTEKST, korzystając z dostępnych narzędzi MCP."
        )
        lines.append(f"3. {f.respond}")
        lines.append(
            "4. Jeśli problem NIE jest rozwiązany i wymaga ponownego sprawdzenia "
            "— użyj schedule_followup aby zaplanować kolejną kontynuację."
        )

        finish_parts = [p for p in [f.transition_done, f.assign_back] if p]
        if finish_parts:
            lines.append("5. Jeśli zadanie zakończone:")
            for part in finish_parts:
                lines.append(f"   - {part}")

        return "\n".join(lines)
