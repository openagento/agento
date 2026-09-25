"""Reconstruct executable CLI commands from completed jobs."""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from .database_config import DatabaseConfig
from .harness import HarnessRunContext, RunRequest, find_harness, get_harness
from .job_models import Job


@dataclass
class ReplayCommand:
    """A reconstructed CLI command for a job."""
    args: list[str]
    harness: str
    provider: str
    model: str | None
    prompt: str
    job: Job
    # The harness's own allow-listed config, as resolved for the job's agent_view.
    # Carried so an `--exec` replay runs on the SAME config the command was built from.
    harness_config: dict[str, str] = field(default_factory=dict)

    @property
    def shell_command(self) -> str:
        """Return a shell-safe command string."""
        return " ".join(shlex.quote(a) for a in self.args)


def _resolve_harness(job: Job, harness_override: str | None) -> str:
    """Resolve the harness id from an override or the job record.

    ``job.agent_type`` already stores what is now called the harness id, so the column
    needs no rename — but the value must name a REGISTERED harness rather than a member
    of a closed enum.
    """
    for candidate, source in ((harness_override, "override"), (job.agent_type, "job")):
        if candidate:
            if find_harness(candidate) is None:
                raise ValueError(f"Unknown harness ({source}): {candidate}")
            return candidate
    raise ValueError(
        f"Job {job.id} has no agent_type recorded. "
        f"Use --credential to specify which harness to use."
    )


def _resolve_provider(registered, job: Job, provider_override: str | None) -> str:
    """The provider to replay on: explicit override, then the job's own, then the default.

    A job recorded before ``job.provider`` existed leaves it NULL — the harness default is
    the only honest guess there. An override that the harness does not offer raises.
    """
    if provider_override:
        if registered.descriptor.provider(provider_override) is None:
            raise ValueError(
                f"Harness {registered.descriptor.id!r} does not offer provider "
                f"{provider_override!r}"
            )
        return provider_override
    if job.provider and registered.descriptor.provider(job.provider) is not None:
        return str(job.provider)
    return str(registered.descriptor.default_provider)


def _resolve_harness_config(job: Job, registered) -> dict[str, str]:
    """The harness's own allow-listed config for the job's agent_view — or refuse.

    Fail closed: without it the built command silently differs from the run it claims
    to reproduce (codex would regain ``--dangerously-bypass-approvals-and-sandbox``,
    pi its built-in tools), and a replay that quietly opens the network is worse than
    no replay at all.
    """
    if job.agent_view_id is None:
        raise ValueError(
            f"Job {job.id} has no agent_view_id, so the harness's own config "
            f"(e.g. codex sandbox_mode) cannot be resolved. Refusing to build a replay "
            f"command that may differ from the run it claims to reproduce."
        )
    from .config_resolver import ScopedConfigService
    from .db import get_connection
    from .harness import get_harness_config
    from .scoped_config import Scope

    conn = get_connection(DatabaseConfig.from_env())
    try:
        svc = ScopedConfigService(conn, Scope.AGENT_VIEW, job.agent_view_id)
        return get_harness_config(svc, registered)
    finally:
        conn.close()


def build_replay_command(
    job: Job,
    *,
    harness_override: str | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
    harness_config: dict[str, str] | None = None,
) -> ReplayCommand:
    """Build the CLI command that would reproduce a job's execution.

    Args:
        harness_override: Harness id (e.g. "claude", "codex").
        model_override: Model name override.
        harness_config: Pre-resolved harness config. ``None`` resolves it from the
            job's agent_view, which refuses a job whose ``agent_view_id`` is NULL.

    Raises:
        ValueError: If job has no stored prompt, the harness cannot be resolved, or
            the harness config cannot be resolved for the job.
    """
    if not job.prompt:
        raise ValueError(
            f"Job {job.id} has no stored prompt. "
            f"Only jobs executed after migration 008 have prompts."
        )

    harness = _resolve_harness(job, harness_override)
    model = model_override or job.model
    prompt = job.prompt

    # Flags come from the harness's CommandBuilder — the same one the consumer uses,
    # so a replayed command is byte-identical to what actually ran.
    registered = get_harness(harness)
    # Replay the provider the job ACTUALLY ran on. Falling back to default_provider
    # unconditionally would replay a non-default-provider run on the wrong provider;
    # pre-0.15 rows have no provider recorded, so those still fall back.
    provider = _resolve_provider(registered, job, provider_override)
    if harness_config is None:
        harness_config = _resolve_harness_config(job, registered)
    ctx = HarnessRunContext(
        harness=harness,
        provider=provider,
        model=model,
        credential_required=False,
        harness_config=harness_config,
    )
    cmd = registered.adapter.command_builder.headless(ctx, RunRequest(prompt=prompt, model=model))

    return ReplayCommand(
        args=cmd,
        harness=harness,
        provider=provider,
        model=model,
        prompt=prompt,
        job=job,
        harness_config=harness_config,
    )


def fetch_job_for_replay(job_id: int, config: DatabaseConfig) -> Job:
    """Fetch a job by ID for replay purposes.

    Raises:
        ValueError: If job not found.
    """
    from .db import get_connection

    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job WHERE id = %s", (job_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Job {job_id} not found.")
            return Job.from_row(row)
    finally:
        conn.close()
