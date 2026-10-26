from __future__ import annotations

from ..job_types import JobTypeLike
from .base import Workflow

# Keyed by the type's string id (PRD E3-E5 §4.2), so a module-declared type and a
# built-in live in one map without the registry knowing which is which.
_WORKFLOW_MAP: dict[str, type[Workflow]] = {}


def _key(agent_type: JobTypeLike | str) -> str:
    return agent_type if isinstance(agent_type, str) else agent_type.value


def get_workflow_class(agent_type: JobTypeLike | str) -> type[Workflow]:
    cls = _WORKFLOW_MAP.get(_key(agent_type))
    if cls is None:
        raise ValueError(
            f"Unknown workflow type: {agent_type}. "
            f"Registered: {list(_WORKFLOW_MAP.keys())}. Has bootstrap() been called?"
        )
    return cls


def register_workflow(agent_type: JobTypeLike | str, cls: type[Workflow]) -> None:
    """Register a workflow class for an agent type."""
    _WORKFLOW_MAP[_key(agent_type)] = cls


def clear() -> None:
    """Reset registry (for testing)."""
    _WORKFLOW_MAP.clear()
