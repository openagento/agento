"""Application bootstrap — scan modules, populate registries, resolve config."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .agent_manager.auth import AuthStrategy, clear_auth_strategies, register_auth_strategy
from .agent_manager.models import AgentProvider
from .channels import registry as channel_registry
from .channels.base import Channel
from .cli_invoker import CliInvoker, register_cli_invoker
from .cli_invoker import clear as clear_cli_invokers
from .commands import Command, register_command
from .commands import clear as clear_commands
from .config_resolver import load_db_overrides, read_config_defaults, resolve_module_config
from .config_writer import ConfigWriter, register_config_writer
from .config_writer import clear as clear_config_writers
from .dependency_resolver import resolve_order, validate_dependencies
from .event_manager import Observer, ObserverEntry, get_event_manager
from .event_manager import clear as clear_event_manager
from .events import (
    ModuleLoadedEvent,
    ModuleReadyEvent,
    ModuleRegisterEvent,
    ModuleReloadEvent,
    ModuleShutdownEvent,
)
from .ingress_identity import clear_regex_identity_types, register_regex_identity_type
from .job_models import AgentType
from .module_loader import ModuleManifest, import_class, scan_modules
from .module_status import filter_enabled
from .onboarding import clear as clear_onboardings
from .onboarding import register_onboarding
from .router import Router
from .router_registry import clear as clear_routers
from .router_registry import register_router
from .runner import Runner
from .runner_factory import clear as clear_runners
from .runner_factory import register_runner as register_runner_factory
from .transcript_reader import TranscriptReader, register_transcript_reader
from .transcript_reader import clear as clear_transcript_readers
from .workflows import clear as clear_workflows
from .workflows import register_workflow
from .workflows.base import Workflow
from .workflows.blank import BlankWorkflow
from .workflows.followup import FollowupWorkflow
from .workflows.todo import TodoWorkflow

logger = logging.getLogger(__name__)


CORE_MODULES_DIR = str(Path(__file__).parent.parent / "modules")
USER_MODULES_DIR = "/app/code"

# Canonical identity-type form (fits ingress_identity.identity_type VARCHAR(32)); the module
# manifest validator applies the same check. Linear pattern on a short type name — safe under `re`.
_IDENTITY_TYPE_FORM = re.compile(r"[a-z][a-z0-9_]{0,31}")

# Module config registry — {module_name: {field: value}}
_MODULE_CONFIGS: dict[str, dict[str, Any]] = {}

# Manifest registry — for shutdown hooks and introspection
_MANIFESTS: list[ModuleManifest] = []


def set_module_config(module_name: str, config: Any) -> None:
    """Override module config (for testing or manual setup)."""
    _MODULE_CONFIGS[module_name] = config


def get_module_config(module_name: str) -> dict[str, Any]:
    """Get resolved config for a module. Returns empty dict if module has no config."""
    return _MODULE_CONFIGS.get(module_name, {})


def get_manifests() -> list[ModuleManifest]:
    """Get loaded module manifests (dependency order)."""
    return list(_MANIFESTS)


def dispatch_shutdown() -> None:
    """Dispatch module_shutdown events in reverse dependency order."""
    em = get_event_manager()
    for m in reversed(_MANIFESTS):
        em.dispatch("module_shutdown_before", ModuleShutdownEvent(name=m.name, path=m.path))


def dispatch_reload() -> None:
    """Dispatch module_reload_before in reverse dependency order (per-tick hot-reload, distinct from shutdown)."""
    em = get_event_manager()
    for m in reversed(_MANIFESTS):
        em.dispatch("module_reload_before", ModuleReloadEvent(name=m.name, path=m.path))


def bootstrap(
    core_dir: str = CORE_MODULES_DIR,
    user_dir: str = USER_MODULES_DIR,
    *,
    db_conn=None,
    quiet: bool = False,
) -> list[ModuleManifest]:
    """Scan core + user modules, populate all registries, resolve config.

    Core modules (ship with agento) are loaded first,
    user modules can override them (like Magento app/code/ vs vendor/).
    Clears registries first for idempotent re-bootstrap (supports module add/remove).

    Args:
        db_conn: Optional DB connection for loading core_config_data overrides.
                 When None (e.g. in tests), only ENV + config.json + defaults are used.
        quiet: When True, downgrade the final "loaded N module(s)" log to DEBUG
               (used by the consumer's per-tick re-bootstrap to avoid log spam).
    """
    channel_registry.clear()
    clear_workflows()
    clear_runners()
    clear_config_writers()
    clear_cli_invokers()
    clear_auth_strategies()
    clear_transcript_readers()
    clear_commands()
    clear_onboardings()
    clear_routers()
    clear_regex_identity_types()
    clear_event_manager()
    _MODULE_CONFIGS.clear()
    _MANIFESTS.clear()

    all_scanned = scan_modules(core_dir) + scan_modules(user_dir)
    enabled = filter_enabled(all_scanned)
    validate_dependencies(enabled, all_scanned)
    manifests = resolve_order(enabled)

    # Resolve module configs (3-level fallback)
    db_overrides = load_db_overrides(db_conn)
    for m in manifests:
        if m.config:
            config_defaults = read_config_defaults(m.path)
            resolved = resolve_module_config(m, config_defaults, db_overrides)
            # If module declares a config_class, convert dict to typed dataclass
            config_class_path = m.provides.get("config_class")
            if config_class_path:
                try:
                    cls = import_class(m.path, config_class_path)
                    resolved = cls.from_dict(resolved)
                except Exception:
                    logger.exception(
                        "Failed to load config_class %r from module %s, using dict",
                        config_class_path, m.name,
                    )
            _MODULE_CONFIGS[m.name] = resolved

    em = get_event_manager()

    # Generic, channel-agnostic workflows registered as framework DEFAULTS *before* modules load,
    # so a module that declares the same workflow type in di.json overrides them (last-writer-wins).
    register_workflow(AgentType.BLANK, BlankWorkflow)
    register_workflow(AgentType.TODO, TodoWorkflow)
    register_workflow(AgentType.FOLLOWUP, FollowupWorkflow)

    for m in manifests:
        # Load observers first so they're registered before events fire
        _load_observers(m)

        # Dispatch module_register (module just loaded, before capabilities)
        em.dispatch(
            "module_register_before",
            ModuleRegisterEvent(
                name=m.name, path=m.path, config=_MODULE_CONFIGS.get(m.name, {})
            ),
        )

        # Register capabilities
        _load_channels(m)
        _load_workflows(m)
        _load_runtimes(m)
        _load_config_writers(m)
        _load_cli_invokers(m)
        _load_auth_strategies(m)
        _load_transcript_readers(m)
        _load_commands(m)
        _load_onboarding(m)
        _load_routers(m)
        _load_regex_identity_types(m)

        # Dispatch module_loaded (capabilities registered)
        em.dispatch("module_load_after", ModuleLoadedEvent(name=m.name, path=m.path))

    # Store manifests for shutdown and introspection
    _MANIFESTS.extend(manifests)

    # Dispatch module_ready (all modules loaded, safe to query registries)
    for m in manifests:
        em.dispatch("module_ready_after", ModuleReadyEvent(name=m.name, path=m.path))

    log = logger.debug if quiet else logger.info
    log(
        "Bootstrap: loaded %d module(s): %s",
        len(manifests),
        ", ".join(m.name for m in manifests),
    )
    return manifests


def _load_observers(m: ModuleManifest) -> None:
    for event_name, observer_list in m.observers.items():
        for decl in observer_list:
            try:
                cls = import_class(m.path, decl["class"])
                if not issubclass(cls, Observer):
                    logger.error(
                        "Observer %r from module %s does not implement Observer protocol, skipping",
                        decl.get("name"), m.name,
                    )
                    continue
                entry = ObserverEntry(
                    name=decl["name"],
                    observer_class=cls,
                    order=decl.get("order", 1000),
                )
                get_event_manager().register(event_name, entry)
                logger.debug(
                    "Registered observer %r for event %r from module %s",
                    decl["name"], event_name, m.name,
                )
            except Exception:
                logger.exception(
                    "Failed to load observer %r from module %s",
                    decl.get("name"), m.name,
                )


def _load_channels(m: ModuleManifest) -> None:
    for decl in m.provides.get("channels", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, Channel):
                logger.error(
                    "Channel %r from module %s does not implement Channel protocol, skipping",
                    decl.get("name"), m.name,
                )
                continue
            channel_registry.register_channel(instance)
            logger.debug("Registered channel %r from module %s", decl["name"], m.name)
        except Exception:
            logger.exception("Failed to load channel %r from module %s", decl.get("name"), m.name)


def _load_workflows(m: ModuleManifest) -> None:
    for decl in m.provides.get("workflows", []):
        try:
            cls = import_class(m.path, decl["class"])
            if not issubclass(cls, Workflow):
                logger.error(
                    "Workflow %r from module %s does not extend Workflow, skipping",
                    decl.get("type"), m.name,
                )
                continue
            agent_type = AgentType(decl["type"])
            register_workflow(agent_type, cls)
            logger.debug("Registered workflow %r from module %s", decl["type"], m.name)
        except Exception:
            logger.exception("Failed to load workflow %r from module %s", decl.get("type"), m.name)


def _load_runtimes(m: ModuleManifest) -> None:
    for decl in m.provides.get("runtimes", []):
        try:
            cls = import_class(m.path, decl["class"])
            if not issubclass(cls, Runner):
                logger.error(
                    "Runtime %r from module %s does not implement Runner protocol, skipping",
                    decl.get("provider"), m.name,
                )
                continue

            def _make_factory(runner_cls: type):
                def factory(**kwargs: object):
                    return runner_cls(**kwargs)
                return factory

            provider = AgentProvider(decl["provider"])
            register_runner_factory(provider, _make_factory(cls))
            logger.debug("Registered runtime %r from module %s", decl["provider"], m.name)
        except Exception:
            logger.exception("Failed to load runtime %r from module %s", decl.get("provider"), m.name)


def _load_config_writers(m: ModuleManifest) -> None:
    for decl in m.provides.get("config_writers", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, ConfigWriter):
                logger.error(
                    "ConfigWriter %r from module %s does not implement ConfigWriter protocol, skipping",
                    decl.get("provider"), m.name,
                )
                continue
            provider = AgentProvider(decl["provider"])
            register_config_writer(provider, instance)
            logger.debug("Registered config writer %r from module %s", decl["provider"], m.name)
        except Exception:
            logger.exception("Failed to load config writer %r from module %s", decl.get("provider"), m.name)


def _load_transcript_readers(m: ModuleManifest) -> None:
    for decl in m.provides.get("transcript_readers", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, TranscriptReader):
                logger.error(
                    "TranscriptReader %r from module %s does not implement TranscriptReader protocol, skipping",
                    decl.get("provider"), m.name,
                )
                continue
            provider = AgentProvider(decl["provider"])
            register_transcript_reader(provider, instance)
            logger.debug("Registered transcript reader %r from module %s", decl["provider"], m.name)
        except Exception:
            logger.exception("Failed to load transcript reader %r from module %s", decl.get("provider"), m.name)


def _load_cli_invokers(m: ModuleManifest) -> None:
    for decl in m.provides.get("cli_invokers", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, CliInvoker):
                logger.error(
                    "CliInvoker %r from module %s does not implement CliInvoker protocol, skipping",
                    decl.get("provider"), m.name,
                )
                continue
            provider = AgentProvider(decl["provider"])
            register_cli_invoker(provider, instance)
            logger.debug("Registered CLI invoker %r from module %s", decl["provider"], m.name)
        except Exception:
            logger.exception("Failed to load CLI invoker %r from module %s", decl.get("provider"), m.name)


def _load_auth_strategies(m: ModuleManifest) -> None:
    for decl in m.provides.get("auth_strategies", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, AuthStrategy):
                logger.error(
                    "Auth strategy %r from module %s does not implement AuthStrategy protocol, skipping",
                    decl.get("provider"), m.name,
                )
                continue
            provider = AgentProvider(decl["provider"])
            register_auth_strategy(provider, instance)
            logger.debug("Registered auth strategy %r from module %s", decl["provider"], m.name)
        except Exception:
            logger.exception("Failed to load auth strategy %r from module %s", decl.get("provider"), m.name)


def _load_commands(m: ModuleManifest) -> None:
    for decl in m.provides.get("commands", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, Command):
                logger.error(
                    "Command %r from module %s does not implement Command protocol, skipping",
                    decl.get("name"), m.name,
                )
                continue
            register_command(instance)
            logger.debug("Registered command %r from module %s", decl["name"], m.name)
        except Exception:
            logger.exception("Failed to load command %r from module %s", decl.get("name"), m.name)


def _load_onboarding(m: ModuleManifest) -> None:
    class_path = m.provides.get("onboarding")
    if not class_path:
        return
    try:
        cls = import_class(m.path, class_path)
        register_onboarding(m.name, cls())
        logger.debug("Registered onboarding from module %s", m.name)
    except Exception:
        logger.exception("Failed to load onboarding from module %s", m.name)


def _load_routers(m: ModuleManifest) -> None:
    for decl in m.provides.get("routers", []):
        try:
            cls = import_class(m.path, decl["class"])
            instance = cls()
            if not isinstance(instance, Router):
                logger.error(
                    "Router %r from module %s does not implement Router protocol, skipping",
                    decl.get("name"), m.name,
                )
                continue
            register_router(instance, order=decl.get("order", 1000))
            logger.debug("Registered router %r from module %s", decl["name"], m.name)
        except Exception:
            logger.exception("Failed to load router %r from module %s", decl.get("name"), m.name)


def _load_regex_identity_types(m: ModuleManifest) -> None:
    """Register a module's regex-matched ingress identity types (generic, channel-agnostic).

    Reads di.json `regex_identity_types` and registers each entry matching the canonical form
    ^[a-z][a-z0-9_]{0,31}$. A malformed declaration (not a list, or an entry that is non-string /
    wrong shape) is logged and skipped — never raised — so a bad third-party manifest cannot crash
    bootstrap (which also runs on every consumer hot-reload, past setup-time validation).
    """
    declared = m.provides.get("regex_identity_types", [])
    if not isinstance(declared, list):
        logger.error(
            "Module %s declares regex_identity_types as %s, expected an array — ignoring",
            m.name, type(declared).__name__,
        )
        return
    for t in declared:
        if isinstance(t, str) and _IDENTITY_TYPE_FORM.fullmatch(t):
            register_regex_identity_type(t)
            logger.debug("Registered regex identity type %r from module %s", t, m.name)
        else:
            logger.error(
                "Module %s declares an invalid regex_identity_type %r (must match "
                "^[a-z][a-z0-9_]{0,31}$), skipping", m.name, t,
            )
