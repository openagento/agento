"""Job types: the four built-ins plus whatever modules declare (PRD E3-E5 §4.2).

``Job.type`` used to be ``AgentType``, a closed enum. A module that wants its own job
type could not add one without editing the framework (CLS-1). The registry opens it,
while a built-in value still resolves to its ``AgentType`` member, so every shipped
equality, hash and dict-key stays true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .job_models import AgentType

BUILTIN_JOB_TYPES = ("cron", "todo", "followup", "blank")

# Lowercase, underscores, <= 32 chars: it is a DB value, a di.json key and part of an
# identifier an operator reads in the queue.
JOB_TYPE_GRAMMAR = re.compile(r"^[a-z][a-z_]{0,31}$")


class JobTypeUnknown(ValueError):
    """Raised for a value no module declared. A ValueError, like get_channel's."""


@runtime_checkable
class JobTypeLike(Protocol):
    """What ``Job.type`` is typed as. ``AgentType`` and ``ModuleJobType`` both satisfy it."""

    value: str


@dataclass(frozen=True)
class ModuleJobType:
    """A module-declared type. Built-ins are never this - they stay ``AgentType``."""

    value: str
    module: str


_REGISTRY: dict[str, ModuleJobType] = {}


def register_job_type(value: str, *, module: str) -> None:
    if not isinstance(value, str) or not JOB_TYPE_GRAMMAR.match(value):
        raise ValueError(
            f"Invalid job type {value!r} from module {module}: "
            "lowercase letters and underscores, starting with a letter, at most 32 characters"
        )
    if value in BUILTIN_JOB_TYPES:
        raise ValueError(f"Job type {value!r} is a built-in and cannot be redeclared (module {module})")
    existing = _REGISTRY.get(value)
    if existing is not None and existing.module != module:
        raise ValueError(
            f"Job type {value!r} is declared by both {existing.module!r} and {module!r}"
        )
    _REGISTRY[value] = ModuleJobType(value=value, module=module)


def resolve_job_type(value: str) -> JobTypeLike:
    """A built-in resolves to its ``AgentType`` member; a declared one to its ``ModuleJobType``."""
    if value in BUILTIN_JOB_TYPES:
        return AgentType(value)
    declared = _REGISTRY.get(value)
    if declared is None:
        raise JobTypeUnknown(
            f"Unknown job type: {value!r}. Registered: {BUILTIN_JOB_TYPES + tuple(_REGISTRY)}. "
            f"Has bootstrap() been called?"
        )
    return declared


def clear_job_types() -> None:
    """Drop every module declaration. Called by bootstrap() on each (re)load.

    The built-ins are not in the registry, so a failed reload still resolves them.
    """
    _REGISTRY.clear()
