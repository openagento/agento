from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ..channels.base import Channel
from ..harness import Runner, RunRequest, RunResult
from ..job_models import Job


@dataclass
class JobContext:
    """Capabilities provided by the consumer to workflows during execution."""

    config: object  # Module config object (e.g. JiraConfig) resolved via bootstrap
    logger: logging.Logger
    update_reference_id: Callable[[int, str], None]


class Workflow(ABC):
    """Base class for all workflow types."""

    def __init__(self, runner: Runner, logger: logging.Logger):
        self.runner = runner
        self.logger = logger

    @abstractmethod
    def build_prompt(
        self, channel: Channel, reference_id: str, **kwargs: object
    ) -> str: ...

    def execute_job(
        self, channel: Channel, job: Job, context: JobContext
    ) -> RunResult:
        """Extract args from Job and delegate to execute().

        Must always return a RunResult. Each workflow is self-sufficient.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement execute_job()"
        )

    def execute(
        self, channel: Channel, reference_id: str, **kwargs: object
    ) -> RunResult:
        prompt = self.build_prompt(channel, reference_id, **kwargs)
        result = self.runner.execute(RunRequest(prompt=prompt))
        result.prompt = prompt
        self.logger.info(
            f"channel={channel.name} ref={reference_id} "
            f"session_id={result.session_id or '?'} {result.stats_line}"
        )
        return result
