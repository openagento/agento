"""The scheduler workflow: run the prompt of one fire. ``job.context`` is the task header and
the schedule's prompt, as the source wrote them at fire time (``source.fire_spec``)."""
from __future__ import annotations

from agento.framework.channels.base import Channel
from agento.framework.harness import RunResult
from agento.framework.job_models import Job
from agento.framework.workflows.base import JobContext, Workflow


class SchedulerWorkflow(Workflow):
    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        if not job.context:
            raise ValueError(f"Scheduler job {job.id} has no context")
        return self.execute(channel, job.reference_id or "", context=context, prompt=job.context)

    def build_prompt(self, channel: Channel, reference_id: str, **kwargs: object) -> str:
        return str(kwargs["prompt"])
