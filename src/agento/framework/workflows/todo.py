from __future__ import annotations

from ..channels.base import Channel, accepts_config
from ..harness import RunResult
from ..job_models import Job
from .base import JobContext, Workflow


class TodoWorkflow(Workflow):
    """One-shot task execution with full lifecycle."""

    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        if job.reference_id:
            return self.execute(channel, job.reference_id, config=context.config)

        # Work discovery (no reference_id — find next task via channel)
        if not hasattr(channel, "discover_work"):
            raise ValueError(
                f"Channel {channel.name!r} does not support work discovery. "
                f"TODO jobs for this channel must have a reference_id."
            )

        # Discovery talks to the toolbox REST API, which is capability-guarded. A
        # required keyword (no default) makes a channel that forgets it fail here,
        # not with a 401 deep inside the HTTP layer.
        if context.capability_token is None:
            raise ValueError(
                f"Work discovery for channel {channel.name!r} needs an internal_rest "
                f"capability, but the job carries none. Discovery jobs are minted one "
                f"only when they have no reference_id and belong to an agent_view."
            )

        items = channel.discover_work(
            context.config, self.logger, capability_token=context.capability_token
        )
        if not items:
            # No session was started, so session_id stays None — it must only ever
            # carry a real session id (it is persisted to job.session_id and drives resume).
            return RunResult(raw_output="No TODO tasks found")

        item = items[0]
        self.logger.info(f"Dispatching: {item.reference_id} - {item.title}")
        context.update_reference_id(job.id, item.reference_id)
        return self.execute(channel, item.reference_id, config=context.config)

    def build_prompt(
        self, channel: Channel, reference_id: str, **kwargs: object
    ) -> str:
        # Forward config only to channels that declare it — a third-party channel on the
        # pre-config interface would otherwise raise TypeError during prompt construction.
        if accepts_config(channel.get_prompt_fragments):
            f = channel.get_prompt_fragments(reference_id, config=kwargs.get("config"))
        else:
            f = channel.get_prompt_fragments(reference_id)
        step = 0

        intro = f.task_intro or f"Wykonaj zadanie ({channel.name}) {reference_id}."
        lines = [
            f"{intro} Postępuj krok po kroku:",
            "",
        ]

        step += 1
        lines.append(f"KROK {step}: Wczytaj zadanie")
        lines.append(f"- {f.read_context}")
        lines.append("")

        if f.transition_start:
            step += 1
            lines.append(f'KROK {step}: Zmień status na "In Progress"')
            lines.append(f"- {f.transition_start}")
            lines.append("")

        if f.ask_and_handback:
            step += 1
            next_step = step + 1
            lines.append(f"KROK {step}: Oceń zadanie")
            lines.append(f"- {f.ask_and_handback}")
            lines.append(f"- Jeśli nie masz pytań — przejdź do KROKU {next_step}.")
            lines.append("")

        step += 1
        lines.append(f"KROK {step}: Zaplanuj i wykonaj zadanie")
        lines.append("- Zaplanuj krok po kroku realizację na podstawie opisu.")
        lines.append("- Wykonaj korzystając z dostępnych narzędzi MCP.")
        lines.append("")

        step += 1
        lines.append(f"KROK {step}: Zwróć wynik")
        lines.append(f"- {f.respond}")

        finish_parts = [p for p in [f.transition_done, f.assign_back] if p]
        if finish_parts:
            step += 1
            lines.append("")
            lines.append(f"KROK {step}: Zakończ")
            for part in finish_parts:
                lines.append(f"- {part}")

        if f.extra:
            lines.append("")
            lines.append(f.extra)

        return "\n".join(lines)
