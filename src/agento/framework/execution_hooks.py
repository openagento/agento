"""Four seams between the queue and whatever module records what a run did (PRD E3-E5 §5.1).

The framework owns the job. A module owns the *execution* - the row that says which attempt
produced which output, which session it resumed, and what it streamed. These four protocols
are how the second reaches the first without the first knowing its name (PLC-2).

At most one implementation of each may be registered, and `module:validate` refuses a second
before any of it runs. With none registered every seam falls back to today's behaviour: a
`None` execution id, no finalize write, the shipped attempt-based resume rule, and deltas
discarded. That is the disableable-module rule (MOD-2) stated as four defaults.

Each implementation is handed the framework's **open connection**, so its rows commit with
the transition that produced them - and the framework itself writes no module table.
"""
from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class RunProfile:
    """What drives one attempt: ids and a model name, never a credential value (SEC-6)."""

    harness: str
    provider: str
    model: str | None
    credential_id: int | None


@runtime_checkable
class ExecutionIdProvider(Protocol):
    """Mints the id one attempt is known by. Returns None to decline this run."""

    def mint(self, *, conn, job_id: int, attempt: int,
             profile: RunProfile | None = None) -> str | None: ...


@runtime_checkable
class ExecutionFinalizer(Protocol):
    """Closes the execution a transition ends. `job_terminal` separates a retry from an end."""

    def finalize(self, *, conn, job_id: int, attempt: int, execution_id: str | None,
                 outcome: Literal["succeeded", "failed", "abandoned"],
                 job_terminal: bool) -> None: ...


@runtime_checkable
class ResumeSessionResolver(Protocol):
    """The harness session this attempt should resume, or None for the shipped rule."""

    def resolve(self, *, conn, job_id: int, attempt: int) -> str | None: ...


@dataclass(frozen=True)
class DeltaRecord:
    """One streamed fragment. Framework-owned and vendor-free: `kind` is this vocabulary,
    not a harness's - a reader must not have to know which CLI produced it."""

    execution_id: str
    seq: int
    kind: str                 # FRAGMENT_KINDS (consumer.py) | "gap" | "truncated"
    text: str | None
    tool_name: str | None
    data: Mapping[str, str | bool] | None = None   # tool fields: call_id, input, output, is_error


@runtime_checkable
class ExecutionDeltaSink(Protocol):
    """Takes a batch of deltas. The only seam of the four that gets no connection: it is
    called from the reading path, which must not hold the run's transaction open."""

    def write(self, batch: Sequence[DeltaRecord]) -> None: ...


class _Slot:
    """One at-most-one registry. Named, so an error says which seam and which module."""

    def __init__(self, seam: str, protocol: type) -> None:
        self._seam = seam
        self._protocol = protocol
        self._impl: object | None = None
        self._module: str | None = None

    def register(self, impl: object, *, module: str) -> None:
        if not isinstance(impl, self._protocol):
            raise TypeError(
                f"module {module}: {type(impl).__name__} does not implement {self._seam}"
            )
        if self._impl is not None and self._module != module:
            raise ValueError(
                f"module {module}: {self._seam} is already provided by {self._module}"
            )
        self._impl, self._module = impl, module

    def get(self) -> object | None:
        return self._impl

    def module(self) -> str | None:
        return self._module

    def clear(self) -> None:
        self._impl = self._module = None


_EXECUTION_ID_PROVIDER = _Slot("execution_id_provider", ExecutionIdProvider)
_EXECUTION_FINALIZER = _Slot("execution_finalizer", ExecutionFinalizer)
_RESUME_SESSION_RESOLVER = _Slot("resume_session_resolver", ResumeSessionResolver)
_EXECUTION_DELTA_SINK = _Slot("execution_delta_sink", ExecutionDeltaSink)

SEAMS = {
    "execution_id_provider": _EXECUTION_ID_PROVIDER,
    "execution_finalizer": _EXECUTION_FINALIZER,
    "resume_session_resolver": _RESUME_SESSION_RESOLVER,
    "execution_delta_sink": _EXECUTION_DELTA_SINK,
}


def register_execution_id_provider(provider: ExecutionIdProvider, *, module: str) -> None:
    _EXECUTION_ID_PROVIDER.register(provider, module=module)


def register_execution_finalizer(finalizer: ExecutionFinalizer, *, module: str) -> None:
    _EXECUTION_FINALIZER.register(finalizer, module=module)


def register_resume_session_resolver(resolver: ResumeSessionResolver, *, module: str) -> None:
    _RESUME_SESSION_RESOLVER.register(resolver, module=module)


def register_execution_delta_sink(sink: ExecutionDeltaSink, *, module: str) -> None:
    _EXECUTION_DELTA_SINK.register(sink, module=module)


def clear() -> None:
    """Empty all four. Called by `bootstrap()`: the consumer re-bootstraps every idle poll
    tick, and a registry that only ever appended would see two providers on the second."""
    for slot in SEAMS.values():
        slot.clear()


def mint_execution_id(*, conn, job_id: int, attempt: int,
                      profile: RunProfile | None = None) -> str | None:
    provider = _EXECUTION_ID_PROVIDER.get()
    if provider is None:
        return None
    # `profile` came after the seam shipped: a provider without the parameter still mints
    # (CODE-3 signature check), it only records no profile.
    if profile is not None and "profile" in inspect.signature(provider.mint).parameters:
        return provider.mint(conn=conn, job_id=job_id, attempt=attempt, profile=profile)
    return provider.mint(conn=conn, job_id=job_id, attempt=attempt)


def finalize_execution(*, conn, job_id: int, attempt: int, execution_id: str | None,
                       outcome: Literal["succeeded", "failed", "abandoned"],
                       job_terminal: bool) -> None:
    """Run the registered finalizer in the caller's transaction (§5.3).

    Contract: `conn` runs at READ COMMITTED (`Consumer._db()`). A finalizer's plain reads
    are therefore fresh - they see what a session it waited on committed - and its ranged
    writes take record locks without gaps, which is what keeps them safe at 100-200
    parallel jobs (SCL-1). See DECISIONS.md, 2026-10-10.
    """
    finalizer = _EXECUTION_FINALIZER.get()
    if finalizer is None:
        return
    finalizer.finalize(conn=conn, job_id=job_id, attempt=attempt, execution_id=execution_id,
                       outcome=outcome, job_terminal=job_terminal)


def resolve_resume_session(*, conn, job_id: int, attempt: int) -> str | None:
    resolver = _RESUME_SESSION_RESOLVER.get()
    if resolver is None:
        return None
    return resolver.resolve(conn=conn, job_id=job_id, attempt=attempt)


def write_execution_deltas(batch: Sequence[DeltaRecord]) -> None:
    sink = _EXECUTION_DELTA_SINK.get()
    if sink is None:
        return
    sink.write(batch)
