"""PRD E3-E5 §4.2 - the extensible job-type contract."""

import pytest

from agento.framework.job_models import AgentType, Job
from agento.framework.job_types import (
    BUILTIN_JOB_TYPES,
    JobTypeUnknown,
    ModuleJobType,
    clear_job_types,
    register_job_type,
    resolve_job_type,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_job_types()
    yield
    clear_job_types()


def test_builtins_resolve():
    for value in BUILTIN_JOB_TYPES:
        assert resolve_job_type(value).value == value


def test_unregistered_value_raises():
    with pytest.raises(JobTypeUnknown):
        resolve_job_type("conversation")


def test_module_cannot_redeclare_a_builtin():
    with pytest.raises(ValueError, match="built-in"):
        register_job_type("blank", module="conversation")


def test_two_modules_cannot_claim_one_value():
    register_job_type("conversation", module="conversation")
    with pytest.raises(ValueError, match="both"):
        register_job_type("conversation", module="other")


def test_one_module_may_redeclare_its_own_value():
    """The consumer re-bootstraps every poll tick; the same declaration must not raise."""
    register_job_type("conversation", module="conversation")
    register_job_type("conversation", module="conversation")
    assert resolve_job_type("conversation") == ModuleJobType("conversation", "conversation")


@pytest.mark.parametrize("bad", ["Conversation", "conv-1", "a" * 33, "", "1conv", "conv ation"])
def test_grammar_rejects(bad):
    with pytest.raises(ValueError):
        register_job_type(bad, module="conversation")


# --- compatibility with the shipped enum (CODE-5) ---------------------------------


def test_builtin_resolves_to_the_enum_member_itself():
    assert resolve_job_type("cron") is AgentType.CRON
    assert resolve_job_type("todo") is AgentType.TODO
    assert resolve_job_type("followup") is AgentType.FOLLOWUP
    assert resolve_job_type("blank") is AgentType.BLANK


def test_from_row_keeps_enum_equality():
    job = Job.from_row(_row(type="cron"))
    assert job.type == AgentType.CRON


def test_resolved_type_still_works_as_a_dict_key():
    mapping = {AgentType.CRON: "x"}
    assert mapping[resolve_job_type("cron")] == "x"


def test_from_row_resolves_a_module_declared_type():
    register_job_type("conversation", module="conversation")
    job = Job.from_row(_row(type="conversation"))
    assert job.type == ModuleJobType("conversation", "conversation")


def test_from_row_fails_closed_on_an_undeclared_type():
    with pytest.raises(JobTypeUnknown):
        Job.from_row(_row(type="conversation"))


def test_module_type_is_not_an_enum_member():
    register_job_type("conversation", module="conversation")
    resolved = resolve_job_type("conversation")
    assert resolved.value == "conversation"
    assert resolved not in set(AgentType)


def _row(**over):
    row = {
        "id": 1, "schedule_id": None, "type": "cron", "source": "cli",
        "agent_view_id": None, "priority": 50, "reference_id": None,
        "agent_type": None, "provider": None, "model": None,
        "input_tokens": None, "output_tokens": None, "prompt": None,
        "output": None, "context": None, "idempotency_key": "k",
        "status": "TODO", "attempt": 1, "max_attempts": 3,
        "scheduled_after": None, "started_at": None, "finished_at": None,
        "result_summary": None, "error_message": None, "error_class": None,
        "pid": None, "session_id": None, "created_at": None, "updated_at": None,
        "requester_key": "k", "requester_email": None, "requester_trust": "claimed",
        "requester_meta": None,
    }
    row.update(over)
    return row
