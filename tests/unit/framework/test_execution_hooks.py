"""The four execution seams: at most one each, and nothing registered is today's behaviour."""
from __future__ import annotations

import dataclasses

import pytest

from agento.framework.execution_hooks import (
    SEAMS,
    DeltaRecord,
    clear,
    finalize_execution,
    mint_execution_id,
    register_execution_delta_sink,
    register_execution_finalizer,
    register_execution_id_provider,
    register_resume_session_resolver,
    resolve_resume_session,
    write_execution_deltas,
)


@pytest.fixture(autouse=True)
def _empty():
    clear()
    yield
    clear()


class Provider:
    def mint(self, *, conn, job_id, attempt):
        return f"x-{job_id}-{attempt}"


class Finalizer:
    def __init__(self):
        self.calls = []

    def finalize(self, *, conn, job_id, attempt, execution_id, outcome, job_terminal):
        self.calls.append((job_id, attempt, execution_id, outcome, job_terminal))


class Resolver:
    def resolve(self, *, conn, job_id, attempt):
        return "session-9"


class Sink:
    def __init__(self):
        self.batches = []

    def write(self, batch):
        self.batches.append(list(batch))


class NotAProvider:
    def something_else(self):
        return None


# --- nothing registered is the framework's own behaviour (MOD-2) -----------

def test_with_no_provider_the_execution_id_is_none():
    assert mint_execution_id(conn=None, job_id=1, attempt=1) is None


def test_with_no_finalizer_nothing_is_written():
    finalize_execution(conn=None, job_id=1, attempt=1, execution_id=None,
                       outcome="succeeded", job_terminal=True)   # must not raise


def test_with_no_resolver_the_shipped_rule_stands():
    assert resolve_resume_session(conn=None, job_id=1, attempt=2) is None


def test_with_no_sink_deltas_are_discarded():
    write_execution_deltas([DeltaRecord("e", 1, "delta", "hi", None)])   # must not raise


# --- one each -------------------------------------------------------------

def test_a_registered_provider_mints():
    register_execution_id_provider(Provider(), module="conversation")

    assert mint_execution_id(conn=None, job_id=7, attempt=2) == "x-7-2"


def test_a_registered_finalizer_is_called_with_the_transition():
    finalizer = Finalizer()
    register_execution_finalizer(finalizer, module="conversation")

    finalize_execution(conn=None, job_id=7, attempt=2, execution_id="e",
                       outcome="failed", job_terminal=False)

    assert finalizer.calls == [(7, 2, "e", "failed", False)]


def test_a_registered_resolver_and_sink_are_reached():
    register_resume_session_resolver(Resolver(), module="conversation")
    sink = Sink()
    register_execution_delta_sink(sink, module="conversation")
    record = DeltaRecord("e", 1, "delta", "hi", None)

    assert resolve_resume_session(conn=None, job_id=1, attempt=2) == "session-9"
    write_execution_deltas([record])

    assert sink.batches == [[record]]


def test_a_second_module_cannot_take_a_seam():
    register_execution_id_provider(Provider(), module="conversation")

    with pytest.raises(ValueError) as exc:
        register_execution_id_provider(Provider(), module="other")

    assert "already provided by conversation" in str(exc.value)


def test_one_module_may_re_register_its_own_seam():
    """The consumer re-bootstraps every idle poll tick; that must not be a collision."""
    register_execution_id_provider(Provider(), module="conversation")
    register_execution_id_provider(Provider(), module="conversation")

    assert mint_execution_id(conn=None, job_id=1, attempt=1) == "x-1-1"


def test_a_class_that_does_not_implement_the_protocol_is_refused():
    with pytest.raises(TypeError) as exc:
        register_execution_id_provider(NotAProvider(), module="conversation")

    assert "does not implement execution_id_provider" in str(exc.value)


def test_clear_empties_every_seam():
    register_execution_id_provider(Provider(), module="conversation")
    register_execution_finalizer(Finalizer(), module="conversation")
    register_resume_session_resolver(Resolver(), module="conversation")
    register_execution_delta_sink(Sink(), module="conversation")

    clear()

    assert [s.get() for s in SEAMS.values()] == [None, None, None, None]


def test_a_delta_record_is_frozen_and_vendor_free():
    record = DeltaRecord("e", 1, "delta", "hi", None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.seq = 2  # type: ignore[misc]
    assert set(record.__dataclass_fields__) == {
        "execution_id", "seq", "kind", "text", "tool_name"}
