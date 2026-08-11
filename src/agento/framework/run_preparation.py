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
) -> tuple[Path | None, Path | None]:
    """Prepare ``(home_dir, working_dir)`` for one run.

    Dispatches ``workspace_build_check_before`` (re-raising ``event.error``),
    creates the per-run artifacts dir, copies the current build into it, and
    materializes the selected credential into the per-run HOME. With
    ``purge_credentials`` and no credential it instead REMOVES any credential state the
    copied build carried, so a credential-free interactive run really is credential-free.
    Falls back to a fresh ``WorkspaceAdapter.prepare_workspace`` when no build
    exists yet.

    ``run_id`` is the job id (``int``) for the consumer or a unique string
    for ``agento run``. Pass ``int`` job ids to get per-job
    ``inject_runtime_params``; ``str`` ids skip injection (interactive run
    uses the build's baked ``.mcp.json``).

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
    state_build_root: Path | None = None
    if current_build is not None:
        # int job ids drive per-job .mcp.json injection; str run ids skip it.
        inject_id = run_id if isinstance(run_id, int) else None
        copy_build_to_artifacts_dir(
            current_build, artifacts_dir,
            job_id=inject_id,
            harness=runtime.harness,
        )
        state_build_root = _build_root_for_current_build(
            current_build,
            runtime.workspace.code,
            runtime.agent_view.code,
        )
    elif runtime.harness:
        from .harness import get_agent_config, workspace_adapter_for
        agent_config = get_agent_config(agent_config_svc) if agent_config_svc else {}
        writer = workspace_adapter_for(runtime.harness)
        writer.prepare_workspace(
            artifacts_dir, agent_config,
            agent_view_id=runtime.agent_view.id,
            toolbox_url=toolbox_url,
        )

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
