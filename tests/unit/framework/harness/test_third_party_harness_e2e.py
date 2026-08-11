"""A harness the framework has never heard of must survive a real job, not just registration.

The earlier suite only asserted that the fixture harness *registers*. That is the cheap
half: a third-party harness can register successfully and still blow up the moment a job
executes, because nothing had checked that its runner satisfies the contract the workflow
calls into. These tests drive ``fake_local`` — a **credential-less** provider — through
the workflow layer and the usage recorder.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import (
    HarnessRunContext,
    Runner,
    RunRequest,
    clear,
    create_runner,
    get_harness,
    parse_harness_declarations,
    register_harness,
    resolve_provider,
)
from agento.framework.module_loader import import_class

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"


@pytest.fixture(autouse=True)
def fake_harness():
    clear()
    module_dir = FIXTURES / "fake_harness"
    for decl in parse_harness_declarations(module_dir / "di.json", "fake_harness"):
        register_harness(decl.descriptor, import_class(module_dir, decl.class_path)())
    yield
    clear()


def _ctx(provider: str = "fake_local", **kwargs) -> HarnessRunContext:
    desc = resolve_provider("fake", provider)
    defaults = dict(
        harness="fake",
        provider=provider,
        credential_required=desc.credential_required,
        working_dir="/workspace",
    )
    defaults.update(kwargs)
    return HarnessRunContext(**defaults)


class TestRunnerContract:
    def test_adapter_runner_satisfies_the_runner_protocol(self):
        runner = create_runner("fake", _ctx())
        assert isinstance(runner, Runner)

    def test_runner_is_not_required_to_subclass_the_shipped_base(self):
        """A harness that talks HTTP instead of spawning a CLI must be able to comply."""
        from agento.framework.harness import SubprocessRunner

        assert not isinstance(create_runner("fake", _ctx()), SubprocessRunner)

    def test_execute_returns_a_populated_runresult(self):
        result = create_runner("fake", _ctx()).execute(RunRequest(prompt="do the thing"))

        assert result.harness == "fake"
        assert result.provider == "fake_local"
        assert result.session_id == "fake-session-1"
        assert (result.input_tokens, result.output_tokens) == (11, 7)
        assert "do the thing" in result.raw_output

    def test_resume_is_just_a_request_carrying_a_session_id(self):
        result = create_runner("fake", _ctx()).execute(
            RunRequest(prompt="", session_id="sess-prev"),
        )
        assert result.session_id == "sess-prev"


class TestWorkflowDrivesIt:
    """The workflow layer types against the protocol, so it must accept this runner."""

    def test_workflow_executes_a_job_on_the_third_party_harness(self):
        from agento.framework.job_models import AgentType, Job, JobStatus
        from agento.framework.workflows.base import JobContext
        from agento.framework.workflows.blank import BlankWorkflow

        runner = create_runner("fake", _ctx())
        workflow = BlankWorkflow(runner, MagicMock())

        channel = MagicMock()
        channel.name = "blank"
        channel.get_prompt_fragments.return_value = MagicMock(
            task_intro="Do it.", steps=[], closing=None,
        )
        job = Job(
            id=1, schedule_id=None, type=AgentType.BLANK, source="blank",
            agent_view_id=None, priority=50, reference_id="X-1", agent_type="fake",
            provider="fake_local",
            model=None, input_tokens=None, output_tokens=None, prompt=None, output=None,
            context=None, idempotency_key="k", status=JobStatus.RUNNING, attempt=1,
            max_attempts=3, scheduled_after=None, started_at=None, finished_at=None,
            result_summary=None, error_message=None, error_class=None, pid=None,
            session_id=None, created_at=None, updated_at=None,
        )

        result = workflow.execute_job(
            channel, job, JobContext(config={}, logger=MagicMock(),
                                     update_reference_id=lambda *_a: None),
        )

        assert result.harness == "fake"
        assert runner.calls, "the workflow never actually called the runner"


class TestCredentiallessProvider:
    def test_no_credential_is_required_and_none_is_claimed(self):
        assert resolve_provider("fake", "fake_local").credential_required is False
        assert _ctx().credential is None

    def test_the_same_harness_also_offers_a_credential_bearing_provider(self):
        cloud = resolve_provider("fake", "fake_cloud")
        assert cloud.credential_required is True
        assert cloud.credential_scope == "fake_cloud"

    def test_credentialless_run_records_usage_with_a_null_credential(self, monkeypatch):
        """``usage_log.credential_id`` is nullable precisely so this run is visible."""
        recorded = []
        monkeypatch.setattr(
            "agento.framework.agent_manager.usage_store.record_usage",
            lambda conn, **kw: recorded.append(kw) or 1,
        )

        from agento.framework.harness import SubprocessRunner

        # Exercise the shipped recorder against a credential-less context: the fixture
        # runner deliberately has no DB path of its own.
        class _Recorder(SubprocessRunner):
            def _parse_output(self, raw):  # pragma: no cover - unused here
                raise NotImplementedError

            def _credential_env(self, credential):  # pragma: no cover - unused here
                return {}

        runner = _Recorder(
            context=_ctx(),
            command_builder=get_harness("fake").adapter.command_builder,
            logger=MagicMock(),
        )
        monkeypatch.setattr(runner, "_get_db_connection", lambda: MagicMock())

        from agento.framework.harness import RunResult

        runner._record_usage(RunResult(raw_output="ok", input_tokens=3, output_tokens=2))

        assert recorded[0]["credential_id"] is None
        assert (recorded[0]["harness"], recorded[0]["provider"]) == ("fake", "fake_local")
        assert recorded[0]["tokens_used"] == 5
