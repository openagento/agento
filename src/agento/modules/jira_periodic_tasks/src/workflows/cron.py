from __future__ import annotations

from agento.framework.channels.base import Channel
from agento.framework.harness import RunResult
from agento.framework.job_models import Job
from agento.framework.workflows.base import JobContext, Workflow


class CronWorkflow(Workflow):
    """Recurring task execution. Never changes status or assignee."""

    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        if not job.reference_id:
            raise ValueError(f"Cron job {job.id} has no reference_id")
        return self.execute(channel, job.reference_id)

    def build_prompt(
        self, channel: Channel, reference_id: str, **kwargs: object
    ) -> str:
        f = channel.get_prompt_fragments(reference_id)

        lines = [
            f"Zadanie cykliczne ({channel.name}) {reference_id}. Postępuj krok po kroku:",
            "",
            f"1. {f.read_context}",
            "   Sprawdź ostatnie komentarze — mogą zawierać wyniki poprzednich uruchomień.",
            "2. Wykonaj zadanie korzystając z dostępnych narzędzi MCP.",
            f"3. {f.respond}",
            "",
            "UWAGA: Nie zmieniaj statusu ani assignee — to zadanie cykliczne.",
        ]

        if f.extra:
            lines.append("")
            lines.append(f.extra)

        return "\n".join(lines)
