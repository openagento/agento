"""Application bootstrap — scan modules, populate registries, resolve config."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .channels import registry as channel_registry
from .channels.base import Channel
from .commands import Command, register_command
from .commands import clear as clear_commands
from .config_resolver import load_db_overrides, read_config_defaults, resolve_module_config
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
from .harness import (
    AgentHarnessAdapter,
    DuplicateCredentialScopeError,
    DuplicateHarnessError,
    ObscureRuntimeConfigError,
    parse_harness_declarations,
    register_harness,
)
from .harness import clear as clear_harnesses
from .ingress_identity import clear_regex_identity_types, register_regex_identity_type
from .job_models import AgentType
from .module_discovery import scan_all_modules
from .module_loader import ModuleManifest, import_class
from .module_status import filter_enabled
from .onboarding import clear as clear_onboardings
from .onboarding import register_onboarding
from .router import Router
from .router_registry import clear as clear_routers
from .router_registry import register_router
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
    clear_harnesses()
    clear_commands()
    clear_onboardings()
    clear_routers()
    clear_regex_identity_types()
    clear_event_manager()
    _MODULE_CONFIGS.clear()
    _MANIFESTS.clear()

    # ONE discovery path shared with setup:upgrade and module:validate — it also covers
    # PyPI extensions bind-mounted at /opt/agento-src/<ext>, which are under neither
    # core_dir nor user_dir.
    all_scanned = scan_all_modules(core_dir, user_dir)
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
        _load_agent_harnesses(m)
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


def _load_agent_harnesses(m: ModuleManifest) -> None:
    """Register every ``agent_harnesses`` declaration from one module.

    Replaces the five enum-keyed loaders (runtimes / config_writers / cli_invokers /
    auth_strategies / transcript_readers). The descriptor is built from the declaration
    itself, so the framework knows a harness's metadata without importing its code.
    """
    for decl in parse_harness_declarations(Path(m.path) / "di.json", m.name):
        try:
            cls = import_class(m.path, decl.class_path)
            adapter = cls()
            if not isinstance(adapter, AgentHarnessAdapter):
                logger.error(
                    "Harness %r from module %s does not implement AgentHarnessAdapter, skipping",
                    decl.descriptor.id, m.name,
                )
                continue
            register_harness(
                decl.descriptor,
                adapter,
                decl.module,
                decl.runtime_config_fields,
                dict(getattr(m, "config", {}) or {}),
            )
            logger.debug("Registered harness %r from module %s", decl.descriptor.id, m.name)
        except (
            DuplicateHarnessError,
            DuplicateCredentialScopeError,
            ObscureRuntimeConfigError,
        ):
            # A collision is NOT survivable: swallowing it would silently make the
            # first-registered harness win, and a duplicate credential scope would let
            # one harness serve another's credential pool. `module:validate` catches
            # this before setup:upgrade touches the DB; if it still reaches here, the
            # deployment is misconfigured and must not come up half-wired.
            #
            # A bad `runtime_config_fields` allow-list joins them for the same reason:
            # it is a SECURITY misconfiguration (a secret declared readable at command
            # construction, or a field whose schema cannot prove it is not one). Letting
            # the generic handler below skip the harness would turn that into a confusing
            # "no harness registered" at job time, with the real cause buried in a log
            # line nobody reads. Fail the boot instead.
            logger.exception(
                "Fatal harness registration error for %r from module %s",
                decl.descriptor.id, m.name,
            )
            raise
        except Exception:
            # Other failures (bad import, adapter blows up in __init__) stay per-module:
            # one broken third-party module must not take the whole consumer down.
            logger.exception(
                "Failed to load harness %r from module %s", decl.descriptor.id, m.name
            )


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
