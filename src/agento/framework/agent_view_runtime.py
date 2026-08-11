"""Resolve the runtime profile for an agent_view — harness, provider, model, priority, overrides.

Used by the consumer to configure each job's execution environment before dispatch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config_resolver import ScopedConfigService
from .scoped_config import Scope
from .workspace import AgentView, Workspace, get_agent_view, get_workspace

logger = logging.getLogger(__name__)

DEFAULT_PRIORITY = 50


@dataclass
class AgentViewRuntime:
    agent_view: AgentView | None = None
    workspace: Workspace | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    priority: int = DEFAULT_PRIORITY
    scoped_overrides: dict = field(default_factory=dict)


def resolve_agent_view_runtime(conn, agent_view_id: int | None) -> AgentViewRuntime:
    """Resolve the full runtime profile for a given agent_view.

    Resolution goes through the single ``ScopedConfigService`` (ENV -> scoped DB ->
    config.json), so ``CONFIG__AGENT_VIEW__PROVIDER`` / ``__MODEL`` /
    ``__SCHEDULING__PRIORITY`` env overrides are honored.

    When ``agent_view_id`` is None (or the row is not found) the runtime resolves
    at the global (default) scope so agent-view-less jobs (e.g. blank-source
    tests, single-tenant deployments that run before any agent_view is created)
    still know which provider pool to select from.
    """
    agent_view = get_agent_view(conn, agent_view_id) if agent_view_id is not None else None
    if agent_view_id is not None and agent_view is None:
        logger.warning("agent_view_id=%d not found, falling back to global config", agent_view_id)

    workspace = (
        get_workspace(conn, agent_view.workspace_id) if agent_view is not None else None
    )

    if agent_view is not None:
        svc = ScopedConfigService(
            conn, Scope.AGENT_VIEW, agent_view.id, workspace_id=agent_view.workspace_id,
        )
    else:
        svc = ScopedConfigService(conn, Scope.DEFAULT, 0)

    runtime = AgentViewRuntime(
        agent_view=agent_view,
        workspace=workspace,
        scoped_overrides=svc.overrides,
    )

    priority_raw = svc.get("agent_view/scheduling/priority")
    priority = DEFAULT_PRIORITY
    if priority_raw is not None:
        try:
            priority = max(0, min(100, int(priority_raw)))
        except (ValueError, TypeError):
            logger.warning("Invalid agent_view/scheduling/priority=%r, using default", priority_raw)

    runtime.harness, runtime.provider = _resolve_harness_and_provider(
        conn,
        agent_view_id=agent_view.id if agent_view is not None else None,
        workspace_id=agent_view.workspace_id if agent_view is not None else None,
        harness_default=svc.get("agent_view/harness"),
        provider_default=svc.get("agent_view/provider"),
    )
    runtime.model = svc.get("agent_view/model")
    runtime.priority = priority
    return runtime


def _resolve_harness_and_provider(
    conn,
    *,
    agent_view_id: int | None,
    workspace_id: int | None,
    harness_default: str | None,
    provider_default: str | None,
) -> tuple[str | None, str | None]:
    """Resolve ``(harness, provider)``, honouring the pre-0.15 single-axis config.

    Before the split, ``agent_view/provider`` held what is now the HARNESS
    ("claude"/"codex"). Since the new ``config.json`` always supplies a default
    harness, "harness is unset" never happens — so the fallback has to compare the
    two values' ORIGINS (ENV > agent_view DB > workspace DB > default DB > config.json)
    rather than mere presence. Otherwise an operator with
    ``CONFIG__AGENT_VIEW__PROVIDER=codex`` in ENV would silently get the default
    harness plus a provider that harness does not offer.

    A legacy value is identified structurally — a ``provider`` that names a REGISTERED
    HARNESS — never by a hardcoded "claude"/"codex" literal, which would reintroduce
    in the framework exactly the branch this contract exists to remove (AGENTS.md #6).
    """
    from .harness import find_harness, resolve_provider
    from .scoped_config import ORIGIN_ABSENT, resolve_with_origin

    harness, harness_origin = resolve_with_origin(
        conn, "agent_view/harness",
        agent_view_id=agent_view_id, workspace_id=workspace_id,
        config_json_value=harness_default,
    )
    provider, provider_origin = resolve_with_origin(
        conn, "agent_view/provider",
        agent_view_id=agent_view_id, workspace_id=workspace_id,
        config_json_value=provider_default,
    )

    if harness_origin == ORIGIN_ABSENT and provider_origin == ORIGIN_ABSENT:
        return None, None

    # A provider that is valid for the effective harness is NOT legacy, even when it
    # happens to share a name with another registered harness.
    if harness and provider:
        registered = find_harness(harness)
        if registered is not None and registered.descriptor.provider(provider) is not None:
            return harness, provider

    legacy = find_harness(provider) if provider else None
    if legacy is not None and provider_origin >= harness_origin:
        logger.warning(
            "agent_view/provider=%r is a pre-0.15 harness value; using harness=%r "
            "provider=%r. Migrate with CONFIG__AGENT_VIEW__HARNESS=%s and "
            "CONFIG__AGENT_VIEW__PROVIDER=%s (or config:set the same paths).",
            provider, legacy.descriptor.id, legacy.descriptor.default_provider,
            legacy.descriptor.id, legacy.descriptor.default_provider,
        )
        return str(legacy.descriptor.id), str(legacy.descriptor.default_provider)

    if not harness:
        return None, provider

    # The harness wins: a leftover legacy provider from a weaker scope is IGNORED in
    # favour of the harness's own default, so `(claude, openai)` can never be produced.
    if legacy is not None:
        logger.warning(
            "Ignoring pre-0.15 agent_view/provider=%r — agent_view/harness=%r is set at a "
            "stronger scope. Using its default_provider.", provider, harness,
        )
        return harness, _default_provider(harness)

    if not provider:
        return harness, _default_provider(harness)

    # Not legacy, not valid for this harness → a real misconfiguration. Raise rather
    # than guess (the old code silently fell back to Claude).
    resolve_provider(harness, provider)
    return harness, provider


def _default_provider(harness: str) -> str | None:
    from .harness import find_harness

    registered = find_harness(harness)
    return str(registered.descriptor.default_provider) if registered else None


def resolve_publish_priority(conn, agent_view_id: int | None) -> int:
    """Resolve the priority for a job being published. Returns DEFAULT_PRIORITY if unset."""
    if agent_view_id is None:
        return DEFAULT_PRIORITY
    runtime = resolve_agent_view_runtime(conn, agent_view_id)
    return runtime.priority
