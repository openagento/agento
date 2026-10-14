"""The harness registry from the enabled module manifests, and nothing else: no observer is
loaded and no event is dispatched (EVT-3), no config is resolved. ``bootstrap()`` registers
harnesses through ``load_harnesses``; ``web`` and the runner's run worker, which do not
bootstrap, use ``harness_reloader``."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from ..module_loader import ModuleManifest, import_class
from .manifest import parse_harness_declarations
from .protocols import AgentHarnessAdapter
from .registry import (
    DuplicateCredentialScopeError,
    DuplicateHarnessError,
    ObscureRuntimeConfigError,
    clear,
    register_harness,
)

logger = logging.getLogger(__name__)


def load_harnesses(manifests: Iterable[ModuleManifest]) -> None:
    """Register every ``agent_harnesses`` declaration of ``manifests``.

    The descriptor is built from the declaration itself, so the framework knows a
    harness's metadata without importing its code.
    """
    for m in manifests:
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


def harness_reloader(core_dir: str | None = None, user_dir: str | None = None) -> Callable[[], None]:
    """A function that registers the harnesses of the enabled modules again when
    ``modules.json`` changed (its mtime), so ``module:enable`` needs no restart. A change to
    core module code still needs one (``sys.modules``). One lock: two callers never clear
    the registry at the same time. A failed load is tried again on the next call."""
    from ..bootstrap import CORE_MODULES_DIR, USER_MODULES_DIR
    from ..dependency_resolver import resolve_order
    from ..module_discovery import scan_all_modules
    from ..module_status import _resolve_path, filter_enabled

    lock = threading.Lock()
    seen: list[object] = [object()]  # matches no mtime: the first call loads

    def reload() -> None:
        with lock:
            try:
                mtime: float | None = _resolve_path().stat().st_mtime
            except OSError:
                mtime = None
            if mtime == seen[0]:
                return
            manifests = resolve_order(filter_enabled(
                scan_all_modules(core_dir or CORE_MODULES_DIR, user_dir or USER_MODULES_DIR)))
            clear()
            load_harnesses(manifests)
            seen[0] = mtime

    return reload


__all__ = ["harness_reloader", "load_harnesses"]
