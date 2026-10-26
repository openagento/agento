"""Run a harness's StreamEventMapper over a whole event stream (one copy for every harness)."""
from __future__ import annotations

from collections.abc import Iterable

from agento.framework.harness.protocols import StreamEventMapper


def map_all(mapper: StreamEventMapper, events: Iterable[dict]) -> list[dict]:
    """Every fragment the mapper emits, flattened in stream order."""
    out: list[dict] = []
    for event in events:
        mapped = mapper.map_event(event)
        out += mapped if isinstance(mapped, list) else [mapped] if mapped else []
    return out
