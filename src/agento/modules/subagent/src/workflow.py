"""The subagent workflow: run the prompt a parent job wrote with agent_run.

`job.reference_id` is the parent job id; `job.context` is the prompt. No parent
context is copied (PRD §13): the child sees a short header and the prompt only.
"""
from __future__ import annotations

from agento.framework.channels.base import Channel
from agento.framework.harness import RunResult
from agento.framework.job_models import Job
from agento.framework.workflows.base import JobContext, Workflow


class SubagentWorkflow(Workflow):
    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        if not job.context:
            raise ValueError(f"Subagent job {job.id} has no context")
        return self.execute(channel, job.reference_id or "", context=context, prompt=job.context)

    def build_prompt(self, channel: Channel, reference_id: str, **kwargs: object) -> str:
        return (
            f"Task delegated by Agento job #{reference_id}. "
            "Your final answer is returned to that job as your result."
            f"\n\n{kwargs['prompt']}"
        )
