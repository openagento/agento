from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from ..channels.base import Channel
from ..harness import Runner, RunRequest, RunResult
from ..job_models import Job


@dataclass
class JobContext:
    """Capabilities provided by the consumer to workflows during execution."""

    config: object  # Module config object (e.g. JiraConfig) resolved via bootstrap
    logger: logging.Logger
    update_reference_id: Callable[[int, str], None]
    # Short-lived internal_rest capability for the discovery flow. `repr=False` so a
    # dataclass repr in a log line can never carry it.
    capability_token: str | None = field(default=None, repr=False)
    # The harness session this turn continues (the latest earlier turn of its line, see
    # framework/threads.py), set only when the harness declares `resume`. `None` means a
    # fresh session.
    resume_session_id: str | None = None


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
        self, channel: Channel, reference_id: str, *, context: JobContext | None = None,
        **kwargs: object,
    ) -> RunResult:
        """Run one turn. With `context.resume_session_id` the turn continues that session,
        and `build_prompt` gets `resumed=True`, so a workflow can send only the new part."""
        session = context.resume_session_id if context else None
        # Optional on a runner (RemoteRunner has it): a runner without it resumes the
        # session as given.
        prepare = getattr(self.runner, "prepare_resume", None)
        if session is not None and prepare is not None and not prepare(session):
            # The session is gone (pruned, or made under another HOME): a fresh one, not a
            # turn the agent has no context for.
            self.logger.warning(f"ref={reference_id} session not found; starting a fresh one")
            session = None
        prompt = self.build_prompt(channel, reference_id, resumed=session is not None, **kwargs)
        result = self.runner.execute(RunRequest(prompt=prompt, session_id=session))
        result.prompt = prompt
        self.logger.info(
            f"channel={channel.name} ref={reference_id} resumed={session is not None} "
            f"session_id={result.session_id or '?'} {result.stats_line}"
        )
        return result
