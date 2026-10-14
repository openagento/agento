"""Shared pre-spawn pipeline for jobs (consumer) and interactive runs (``agento run``).

Encapsulates the freshness check + per-run artifacts dir + build copy that
both paths must do identically: claim the credential elsewhere, but
materialize the workspace the same way so a manual ``agento run`` lands in
the same dir layout the consumer prepares for a real job.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts_dir import (
    build_artifacts_dir,
    copy_build_to_artifacts_dir,
    get_current_build_dir,
    prepare_artifacts_dir,
)
from .event_manager import get_event_manager
from .events import WorkspaceBuildCheckEvent
from .persistent_home import ensure_state_dir, link_persistent_paths
from .ssh_identity import (
    ResolvedSshIdentity,
    materialize_ssh_public_identity,
    resolve_ssh_identity,
    scrub_ssh_private_key,
)
from .workspace_paths import BUILD_DIR

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import Callable

    from .agent_manager.models import CredentialRecord
    from .agent_view_runtime import AgentViewRuntime


def _build_root_for_current_build(
    current_build: Path,
    workspace_code: str,
    agent_view_code: str,
) -> Path:
    for parent in current_build.parents:
        if parent.name == agent_view_code and parent.parent.name == workspace_code:
            return parent.parent.parent
    return Path(BUILD_DIR)


def _ensure_private_ssh_dir(artifacts_dir: Path | str) -> None:
    """Make ``artifacts_dir/.ssh`` a REAL 0700 directory, never a symlink.

    ``copy_build_to_artifacts_dir`` symlinks every non-owned directory, so a build made
    before this change leaves the run's ``.ssh`` pointing back into the shared build dir —
    writing an identity through that link would recreate the exact bug this closes.
    """
    ssh_dir = Path(artifacts_dir) / ".ssh"
    if ssh_dir.is_symlink():
        ssh_dir.unlink()
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    ssh_dir.chmod(0o700)


def check_workspace_build(em, agent_view_id: int) -> None:
    """Dispatch ``workspace_build_check_before`` and re-raise ``event.error``."""
    check_event = WorkspaceBuildCheckEvent(agent_view_id=agent_view_id)
    em.dispatch("workspace_build_check_before", check_event)
    if check_event.error is not None:
        raise check_event.error


def materialize_run_workspace(
    runtime: AgentViewRuntime,
    *,
    run_id: int | str,
    agent_config_svc=None,
    toolbox_url: str = "http://toolbox:3001",
    em=None,
    credential: CredentialRecord | None = None,
    purge_credentials: bool = False,
    effective_model: str | None = None,
    capability_token: str | None = None,
    ssh_identity: ResolvedSshIdentity | None = None,
    check_build: Callable[[object, int], None] | None = None,
) -> tuple[Path | None, Path | None]:
    """Prepare ``(home_dir, working_dir)`` for one run.

    Dispatches ``workspace_build_check_before`` (re-raising ``event.error``),
    creates the per-run artifacts dir, copies the current build into it, and
    materializes the selected credential into the per-run HOME. With
    ``purge_credentials`` and no credential it instead REMOVES any credential state the
    copied build carried, so a credential-free interactive run really is credential-free.
    Falls back to a fresh ``WorkspaceAdapter.prepare_workspace`` when no build
    exists yet.

    ``ssh_identity`` is the run's already-resolved SSH identity (the spawn path resolves
    it once and also needs it for the env). Only the NON-SECRET parts are written; the
    private key is never a file. ``None`` resolves it here from ``agent_config_svc``.

    ``check_build(em, agent_view_id)`` replaces the plain check; the consumer passes one
    that remembers a passed check for one poll interval.

    ``run_id`` is the job id (``int``) for the consumer or a unique string for
    ``agento run``. An ``int`` id scopes the run to a job via
    ``inject_runtime_params``; a ``str`` id scopes it to the run itself, so the toolbox
    can still give it a desk of its own. Injection also runs when a per-run override has
    to reach the harness's config, and when the run carries a ``capability_token`` — the
    build itself carries no claims, so a run without one reaches the toolbox with none.
    Each is passed only to an adapter that names the keyword, so an adapter predating
    either keeps the build's baked config as-is.

    Returns ``(None, None)`` when ``runtime`` carries no agent_view/workspace
    (blank jobs), mirroring the consumer guard.
    """
    if runtime.agent_view is None or runtime.workspace is None:
        return None, None

    (check_build or check_workspace_build)(em or get_event_manager(), runtime.agent_view.id)

    artifacts_dir = build_artifacts_dir(
        runtime.workspace.code, runtime.agent_view.code, run_id,
    )
    prepare_artifacts_dir(artifacts_dir)

    current_build = get_current_build_dir(
        runtime.workspace.code, runtime.agent_view.code,
    )
    # The per-run model: an explicit override (`--model`) wins over the agent_view's
    # configured value. Resolved for BOTH branches — while this lived inside the
    # `current_build` branch the no-build fallback never saw an override at all.
    effective_model = effective_model or getattr(runtime, "model", None)

    state_build_root: Path | None = None
    if current_build is not None:
        # int job ids scope the run to a job; a str run id (`agento run`) has no job
        # scope, so it scopes by its own run id instead — the same unique segment this
        # run's artifacts dir already ends with — and still needs the override applied.
        inject_id = run_id if isinstance(run_id, int) else None
        inject_run = run_id if isinstance(run_id, str) else None
        copy_build_to_artifacts_dir(
            current_build, artifacts_dir,
            job_id=inject_id,
            run_id=inject_run,
            capability_token=capability_token,
            toolbox_url=toolbox_url,
            harness=runtime.harness,
            effective_model=effective_model,
            effective_provider=getattr(runtime, "provider", None),
        )
        state_build_root = _build_root_for_current_build(
            current_build,
            runtime.workspace.code,
            runtime.agent_view.code,
        )
    elif runtime.harness:
        from .harness import (
            get_agent_config,
            get_harness,
            get_harness_config,
            supply_harness_config,
            workspace_adapter_for,
        )
        agent_config = get_agent_config(agent_config_svc) if agent_config_svc else {}
        # There is no build to inject into on this path, so the override has to reach the
        # adapter through the config it materializes FROM — otherwise a `--model` run
        # bakes the configured model as its expectation and the guard fails a legitimate
        # override on the one path that has no second chance to correct it.
        if effective_model:
            agent_config = {**agent_config, "model": effective_model}
        writer = workspace_adapter_for(runtime.harness)
        # The no-build fallback must supply the harness's own allow-listed config too, or
        # settings that depend on it are silently ignored on this path only.
        harness_config = (
            get_harness_config(agent_config_svc, get_harness(runtime.harness))
            if agent_config_svc is not None else {}
        )
        kwargs = supply_harness_config(
            writer,
            {"agent_view_id": runtime.agent_view.id, "toolbox_url": toolbox_url},
            harness_config,
        )
        writer.prepare_workspace(artifacts_dir, agent_config, **kwargs)
        if capability_token is not None:
            # The build-copy path injects via copy_build_to_artifacts_dir; a
            # freshly-prepared workspace must get the same treatment or the run
            # would reach the toolbox with no capability at all.
            writer.inject_runtime_params(
                artifacts_dir,
                job_id=run_id if isinstance(run_id, int) else None,
                run_id=run_id if isinstance(run_id, str) else None,
                capability_token=capability_token,
                toolbox_url=toolbox_url,
            )

    # SSH identity: the NON-SECRET files only. The private key is never written — it is
    # delivered in memory through the per-run env to a per-run ssh-agent (ssh_prelude.py).
    # `ssh_identity` is passed by both spawn paths so the four config values are read ONCE
    # per run; the `None` default resolves here so existing callers stay valid.
    if not Path(artifacts_dir).is_dir():
        # Nothing to scrub and nowhere to write. `prepare_artifacts_dir` above guarantees
        # the dir in production, so this only fires when a caller stubbed it out.
        logger.debug("Run dir %s absent — skipping SSH identity", artifacts_dir)
    else:
        # UNCONDITIONAL, and before anything that depends on config: a build made BEFORE
        # this change can still carry .ssh/id_rsa, which the copy above brought along.
        # Whether this run gets an identity is irrelevant to whether it may read an old
        # key, so neither step may sit behind the config service. Fails closed: a key we
        # cannot remove is a key the agent can read, so the run must not start.
        _ensure_private_ssh_dir(artifacts_dir)
        scrub_ssh_private_key(artifacts_dir)
        if agent_config_svc is None and ssh_identity is None:
            logger.warning(
                "No agent_view config service for this run — it gets no SSH identity, so "
                "git-over-SSH will not authenticate",
            )
        else:
            resolved = (
                ssh_identity if ssh_identity is not None
                else resolve_ssh_identity(agent_config_svc)
            )
            materialize_ssh_public_identity(artifacts_dir, resolved)

    if runtime.harness:
        from .harness import persistent_home_paths_for, workspace_adapter_for
        persistent_paths = persistent_home_paths_for(runtime.harness)
        if persistent_paths:
            state_root = ensure_state_dir(
                runtime.workspace.code,
                runtime.agent_view.code,
                persistent_paths,
                build_root=state_build_root or BUILD_DIR,
            )
            link_persistent_paths(artifacts_dir, state_root, persistent_paths)

        if credential is not None:
            writer = workspace_adapter_for(runtime.harness)
            writer.write_credentials(artifacts_dir, credential)
        elif purge_credentials:
            # The run dir was COPIED from the current build, which may already hold
            # credentials a previous `materialize_agent_credentials` wrote. A deliberately
            # credential-free interactive run must not inherit them: they could belong to a
            # credential that is now disabled, errored or deregistered.
            writer = workspace_adapter_for(runtime.harness)
            writer.remove_credentials(artifacts_dir)

    return artifacts_dir, artifacts_dir
