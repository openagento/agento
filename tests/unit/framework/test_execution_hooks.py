"""The four execution seams: at most one each, and nothing registered is today's behaviour."""
from __future__ import annotations

import dataclasses

import pytest

from agento.framework.execution_hooks import (
    SEAMS,
    DeltaRecord,
    RunProfile,
    clear,
    finalize_execution,
    mint_execution_id,
    record_tool_risk,
    resolve_resume_session,
)
from tests.unit.framework.delta_fakes import FakeSink


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


# --- one each -------------------------------------------------------------

def test_a_registered_provider_mints():
    SEAMS["execution_id_provider"].register(Provider(), module="conversation")

    assert mint_execution_id(conn=None, job_id=7, attempt=2) == "x-7-2"


def test_a_profile_reaches_only_a_provider_that_takes_it():
    """`profile` came after the seam shipped: an older provider still mints (CODE-3)."""
    seen = []

    class Profiled:
        def mint(self, *, conn, job_id, attempt, profile=None):
            seen.append(profile)
            return "p"

    profile = RunProfile(harness="h", provider="p", model=None, credential_id=None)
    SEAMS["execution_id_provider"].register(Provider(), module="conversation")
    assert mint_execution_id(conn=None, job_id=7, attempt=2, profile=profile) == "x-7-2"
    clear()
    SEAMS["execution_id_provider"].register(Profiled(), module="conversation")
    assert mint_execution_id(conn=None, job_id=7, attempt=2, profile=profile) == "p"
    assert seen == [profile]


def test_a_registered_finalizer_is_called_with_the_transition():
    finalizer = Finalizer()
    SEAMS["execution_finalizer"].register(finalizer, module="conversation")

    finalize_execution(conn=None, job_id=7, attempt=2, execution_id="e",
                       outcome="failed", job_terminal=False)

    assert finalizer.calls == [(7, 2, "e", "failed", False)]


def test_a_registered_resolver_and_sink_are_reached():
    SEAMS["resume_session_resolver"].register(Resolver(), module="conversation")
    sink = FakeSink()
    SEAMS["execution_delta_sink"].register(sink, module="conversation")

    assert resolve_resume_session(conn=None, job_id=1, attempt=2) == "session-9"
    assert SEAMS["execution_delta_sink"].get() is sink


def test_a_second_module_cannot_take_a_seam():
    SEAMS["execution_id_provider"].register(Provider(), module="conversation")

    with pytest.raises(ValueError) as exc:
        SEAMS["execution_id_provider"].register(Provider(), module="other")

    assert "already provided by conversation" in str(exc.value)


def test_one_module_may_re_register_its_own_seam():
    """The consumer re-bootstraps every idle poll tick; that must not be a collision."""
    SEAMS["execution_id_provider"].register(Provider(), module="conversation")
    SEAMS["execution_id_provider"].register(Provider(), module="conversation")

    assert mint_execution_id(conn=None, job_id=1, attempt=1) == "x-1-1"


def test_a_class_that_does_not_implement_the_protocol_is_refused():
    with pytest.raises(TypeError) as exc:
        SEAMS["execution_id_provider"].register(NotAProvider(), module="conversation")

    assert "does not implement execution_id_provider" in str(exc.value)


def test_clear_empties_every_seam():
    SEAMS["execution_id_provider"].register(Provider(), module="conversation")
    SEAMS["execution_finalizer"].register(Finalizer(), module="conversation")
    SEAMS["resume_session_resolver"].register(Resolver(), module="conversation")
    SEAMS["execution_delta_sink"].register(FakeSink(), module="conversation")

    clear()

    assert [s.get() for s in SEAMS.values()] == [None, None, None, None]


def test_a_delta_record_is_frozen_and_vendor_free():
    record = DeltaRecord("e", 1, "delta", "hi", None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.seq = 2  # type: ignore[misc]
    assert set(record.__dataclass_fields__) == {
        "execution_id", "seq", "kind", "text", "tool_name", "data", "created_at"}


def test_mint_only_provider_registers_and_mints():
    """`record_tool_risk` is optional: a provider with only `mint` still registers, mints,
    and receives no assessment write."""
    SEAMS["execution_id_provider"].register(Provider(), module="m")
    profile = RunProfile(harness="h", provider="p", model=None, credential_id=None, tool_risk={"v": 1})
    assert mint_execution_id(conn=None, job_id=1, attempt=1, profile=profile) == "x-1-1"
    assert record_tool_risk(conn=None, execution_id="x-1-1", tool_risk={"v": 1}) is False


def test_record_tool_risk_reaches_a_provider_that_has_it():
    calls = []

    class Recording(Provider):
        def record_tool_risk(self, *, conn, execution_id, tool_risk):
            calls.append((execution_id, tool_risk))

    SEAMS["execution_id_provider"].register(Recording(), module="m")
    assert record_tool_risk(conn=None, execution_id="e", tool_risk={"v": 1}) is True
    assert calls == [("e", {"v": 1})]


def test_with_no_provider_nothing_is_recorded():
    assert record_tool_risk(conn=None, execution_id="e", tool_risk={}) is False


def test_tool_risk_defaults_to_none():
    assert RunProfile(harness="h", provider="p", model=None, credential_id=None).tool_risk is None
