"""Shared pre-spawn pipeline for jobs (consumer) and interactive runs (``agento run``).

Encapsulates the freshness check + per-run artifacts dir + build copy that
both paths must do identically: claim the credential elsewhere, but
materialize the workspace the same way so a manual ``agento run`` lands in
the same dir layout the consumer prepares for a real job.
"""
from __future__ import annotations

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
from .workspace_paths import BUILD_DIR

if TYPE_CHECKING:
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
) -> tuple[Path | None, Path | None]:
    """Prepare ``(home_dir, working_dir)`` for one run.

    Dispatches ``workspace_build_check_before`` (re-raising ``event.error``),
    creates the per-run artifacts dir, copies the current build into it, and
    materializes the selected credential into the per-run HOME. With
    ``purge_credentials`` and no credential it instead REMOVES any credential state the
    copied build carried, so a credential-free interactive run really is credential-free.
    Falls back to a fresh ``WorkspaceAdapter.prepare_workspace`` when no build
    exists yet.

    ``run_id`` is the job id (``int``) for the consumer or a unique string for
    ``agento run``. An ``int`` id scopes the run to a job via
    ``inject_runtime_params``. A ``str`` id has no job scope, so nothing is scoped —
    but injection still runs when a per-run override has to reach the harness's config,
    and only for an adapter that names an ``effective_*`` keyword. Without an override a
    ``str`` id is not injected at all and the build's baked config is used as-is.

    Returns ``(None, None)`` when ``runtime`` carries no agent_view/workspace
    (blank jobs), mirroring the consumer guard.
    """
    if runtime.agent_view is None or runtime.workspace is None:
        return None, None

    event_manager = em or get_event_manager()

    check_event = WorkspaceBuildCheckEvent(agent_view_id=runtime.agent_view.id)
    event_manager.dispatch("workspace_build_check_before", check_event)
    if check_event.error is not None:
        raise check_event.error

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
        # scope, but still needs the per-run override applied.
        inject_id = run_id if isinstance(run_id, int) else None
        copy_build_to_artifacts_dir(
            current_build, artifacts_dir,
            job_id=inject_id,
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
