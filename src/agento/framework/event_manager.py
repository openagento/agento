"""Event manager — Magento-style event-observer pattern."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Observer(Protocol):
    """Observer interface — implement execute() to handle events."""

    def execute(self, event: object) -> None: ...


@dataclass
class ObserverEntry:
    """Registration record for an observer bound to an event."""

    name: str
    observer_class: type
    order: int = 1000


class EventManager:
    """Synchronous event dispatcher with ordered observers."""

    def __init__(self) -> None:
        self._observers: dict[str, list[ObserverEntry]] = defaultdict(list)

    def register(self, event_name: str, entry: ObserverEntry) -> None:
        """Register an observer for an event. Re-sorts by (order, name)."""
        self._observers[event_name].append(entry)
        self._observers[event_name].sort(key=lambda e: (e.order, e.name))

    def dispatch(self, event_name: str, event: object) -> None:
        """Instantiate each observer and call execute(). Errors swallowed and logged."""
        for entry in self._observers.get(event_name, []):
            try:
                observer = entry.observer_class()
                observer.execute(event)
            except Exception:
                logger.exception(
                    "Observer %r failed for event %r",
                    entry.name,
                    event_name,
                )

    def dispatch_with_result(self, event_name: str, event: object) -> bool:
        """Dispatch, and tell the caller whether every observer completed.

        `dispatch()` swallows a failure because most events are notifications: a broken
        observer must not break the job it is watching. This one hands the failure back,
        for the caller that must treat "the observer could not decide" as a decision of its
        own (PRD E3-E5 §4.4). It is otherwise the same dispatch, in the same order.
        """
        ok = True
        for entry in self._observers.get(event_name, []):
            try:
                entry.observer_class().execute(event)
            except Exception:
                ok = False
                logger.exception(
                    "Observer %r failed for event %r", entry.name, event_name,
                )
        return ok

    def observer_count(self, event_name: str) -> int:
        return len(self._observers.get(event_name, []))


# Module-level registry (matches channel/workflow/runner pattern)
_EVENT_MANAGER: EventManager | None = None


def get_event_manager() -> EventManager:
    global _EVENT_MANAGER
    if _EVENT_MANAGER is None:
        _EVENT_MANAGER = EventManager()
    return _EVENT_MANAGER


def clear() -> None:
    global _EVENT_MANAGER
    _EVENT_MANAGER = None
