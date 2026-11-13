"""Schedule sources (AG-67 plan §5.4): who owns a ``schedule`` row and what its fire publishes.

A module declares one di.json entry, ``"schedule_sources": [{"name": "<source>", "class":
"src.source.MySource"}]``. ``schedule.source`` names the source. A row whose source is not
registered (its module is disabled, or unknown) is neither fired nor finalized by the tick.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from ..bootstrap import get_manifests
from ..job_models import JobRequester
from ..module_loader import import_class

logger = logging.getLogger(__name__)


class FireSpec(NamedTuple):
    """What one fire publishes: the job's channel source, job type, reference and thread."""

    source: str
    agent_type: str
    reference_id: str
    thread: str | None
    requester: JobRequester | None
    context: str | None
    priority: int


@runtime_checkable
class ScheduleSource(Protocol):
    """Optional member, read with ``getattr`` so a source without it keeps loading (CODE-3):
    ``on_delete(cur, row) -> None``, called by ``tick.finalize_delete`` in the deleting
    transaction, before the row is removed. It must not commit."""

    @property
    def name(self) -> str: ...  # == schedule.source

    @property
    def editable(self) -> bool: ...  # may a user or a tool write the row

    def fire_spec(self, cur, row: dict) -> FireSpec: ...


# Declarations of a process that never runs ``bootstrap()`` (``web``), by module name.
_REGISTERED: dict[str, tuple[Path, list]] = {}


def register_module_sources(module: str, path: Path, declarations: list) -> None:
    """Register one enabled module's ``schedule_sources`` without ``bootstrap()``.

    ``web`` calls this from ``routes_registry.load_module_routes`` (as it does for channels
    and job types), so Run now and a panel delete find the row's source there too.
    """
    _REGISTERED[module] = (path, list(declarations))


def _declarations():
    seen = set()
    for m in get_manifests():
        seen.add(m.name)
        yield m.name, m.path, m.provides.get("schedule_sources", [])
    for name, (path, decls) in _REGISTERED.items():
        if name not in seen:
            yield name, path, decls


def load_sources() -> dict[str, ScheduleSource]:
    """Every registered source, by name, from the enabled modules' di.json.

    Built per call from ``get_manifests()`` (filled by ``bootstrap()``) plus the modules
    ``web`` registered, so a hot reload or a disabled module is seen at once. A source that
    fails to load is logged and left out.
    """
    sources: dict[str, ScheduleSource] = {}
    for module, path, decls in _declarations():
        for decl in decls:
            try:
                instance = import_class(path, decl["class"])()
            except Exception:
                logger.exception("Failed to load schedule source %r from module %s", decl.get("name"), module)
                continue
            if not isinstance(instance, ScheduleSource):
                logger.error("Schedule source %r from module %s does not implement ScheduleSource, skipping",
                             decl.get("name"), module)
                continue
            sources[instance.name] = instance
    return sources


def get_source(name: str) -> ScheduleSource | None:
    """The source of that name; None when its module is disabled or unknown."""
    return load_sources().get(name)
