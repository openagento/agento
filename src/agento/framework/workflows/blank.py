from __future__ import annotations

from ..channels.base import Channel
from ..harness import RunResult
from ..job_models import Job
from .base import JobContext, Workflow


class BlankWorkflow(Workflow):
    """Minimal workflow — sends a simple prompt, no MCP instructions."""

    def execute_job(self, channel: Channel, job: Job, context: JobContext) -> RunResult:
        return self.execute(channel, job.reference_id or "BLANK")

    def build_prompt(
        self, channel: Channel, reference_id: str, **kwargs: object
    ) -> str:
        return f"E2E test {reference_id}. Respond with exactly one word: OK"
