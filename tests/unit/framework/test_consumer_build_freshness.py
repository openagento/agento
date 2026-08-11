"""Regression: consumer rebuilds workspace at job-claim time when the resolved
config drifts from the on-disk build.

The bug: after ``config:set agent_view/provider <new>``, ``workspace/build/<ws>/
<view>/current/`` still contained the *old* provider's files. ``_run_job``
trusted the symlink and copied the stale build into the artifacts dir, so the
agent ran with no MCP tools.

The fix: ``_run_job`` dispatches ``workspace_build_check_before`` (consumed by
``BuildFreshnessCheckObserver`` in the ``workspace_build`` module). The
observer calls ``execute_build`` which is idempotent — it short-circuits when
the checksum matches the on-disk build, and rebuilds when anything affecting
the build changed. Errors captured on the event are re-raised by the consumer
so a failing rebuild can't silently fall back to the stale build.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_view_runtime import AgentViewRuntime
from agento.framework.consumer import Consumer
from agento.framework.events import WorkspaceBuildCheckEvent
from agento.framework.job_models import AgentType, Job, JobStatus
from agento.framework.workspace import AgentView, Workspace
from agento.modules.claude.src.output_parser import ClaudeResult

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _make_job(agent_view_id: int | None) -> Job:
    return Job(
        id=42,
        schedule_id=None,
        type=AgentType.CRON,
        source="jira",
        agent_view_id=agent_view_id,
        priority=50,
        reference_id="AI-1",
        agent_type=None,
        provider=None,
        model=None,
        input_tokens=None,
        output_tokens=None,
        prompt=None,
        output=None,
        context=None,
        idempotency_key="jira:cron:AI-1:20260515_0800",
        status=JobStatus.TODO,
        attempt=0,
        max_attempts=3,
        scheduled_after=datetime(2026, 5, 15, 8, 0),
        started_at=None,
        finished_at=None,
        result_summary=None,
        error_message=None,
        error_class=None,
        pid=None,
        session_id=None,
        created_at=datetime(2026, 5, 15, 7, 59),
        updated_at=datetime(2026, 5, 15, 7, 59),
    )


def _runtime_with_agent_view(harness: str = "claude", provider: str = "anthropic") -> AgentViewRuntime:
    now = datetime.now(UTC)
    return AgentViewRuntime(
        agent_view=AgentView(
            id=2, workspace_id=1, code="developer", label="Developer",
            is_active=True, created_at=now, updated_at=now,
        ),
        workspace=Workspace(
            id=1, code="acme", label="Acme",
            is_active=True, created_at=now, updated_at=now,
        ),
        harness=harness,
        provider=provider,
        model="opus-4",
        priority=50,
        scoped_overrides={},
    )


def _claude_result() -> ClaudeResult:
    return ClaudeResult(
        raw_output="ok", input_tokens=10, output_tokens=5,
        cost_usd=0.0, num_turns=1, duration_ms=100,
        session_id="success", harness="claude", prompt=None,
    )


@pytest.fixture(autouse=True)
def _mock_token_resolver():
    with patch("agento.framework.consumer.CredentialResolver") as MockCls:
        mock = MagicMock()
        token = MagicMock()
        token.credentials_path = "/etc/tokens/claude_1.json"
        mock.resolve.return_value = token
        MockCls.return_value = mock
        yield


@patch("agento.framework.run_preparation.copy_build_to_artifacts_dir")
@patch("agento.framework.run_preparation.get_current_build_dir", return_value=None)
@patch("agento.framework.consumer.get_workflow_class")
@patch("agento.framework.consumer.get_channel")
@patch("agento.framework.consumer.create_runner")
@patch("agento.framework.consumer.get_connection")
@patch("agento.framework.harness.persistent_home_paths_for", return_value=[])
@patch("agento.framework.harness.workspace_adapter_for")
@patch("agento.framework.run_preparation.prepare_artifacts_dir")
@patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/developer/runs/42")
@patch("agento.framework.consumer.resolve_agent_view_runtime")
@patch("agento.framework.consumer.get_event_manager")
def test_dispatches_check_with_agent_view_id(
    mock_get_em, mock_resolve, mock_build_artifacts, mock_prepare, mock_get_writer,
    mock_persistent,
    mock_conn, MockRunner, mock_get_ch, mock_get_wf,
    mock_get_current, mock_copy_build,
    sample_db_config, sample_consumer_config,
):
    """_run_job dispatches workspace_build_check_before with the job's agent_view_id."""
    mock_em = MagicMock()
    mock_get_em.return_value = mock_em
    mock_resolve.return_value = _runtime_with_agent_view()
    mock_conn.return_value = MagicMock()
    mock_get_writer.return_value = MagicMock()

    workflow = MagicMock()
    workflow.execute_job.return_value = _claude_result()
    mock_get_wf.return_value.return_value = workflow
    mock_get_ch.return_value = MagicMock(name="jira")

    consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
    consumer._run_job(_make_job(agent_view_id=2))

    dispatch_calls = [
        c for c in mock_em.dispatch.call_args_list
        if c.args and c.args[0] == "workspace_build_check_before"
    ]
    assert len(dispatch_calls) == 1
    event = dispatch_calls[0].args[1]
    assert isinstance(event, WorkspaceBuildCheckEvent)
    assert event.agent_view_id == 2


@patch("agento.framework.consumer.get_workflow_class")
@patch("agento.framework.consumer.get_channel")
@patch("agento.framework.consumer.create_runner")
@patch("agento.framework.consumer.get_connection")
@patch("agento.framework.consumer.resolve_agent_view_runtime")
@patch("agento.framework.consumer.get_event_manager")
def test_skips_dispatch_when_no_agent_view(
    mock_get_em, mock_resolve, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
    sample_db_config, sample_consumer_config,
):
    """Job with no agent_view/workspace skips the freshness check entirely."""
    mock_em = MagicMock()
    mock_get_em.return_value = mock_em
    runtime = AgentViewRuntime()
    runtime.harness = "claude"
    runtime.provider = "anthropic"
    mock_resolve.return_value = runtime
    mock_conn.return_value = MagicMock()

    workflow = MagicMock()
    workflow.execute_job.return_value = _claude_result()
    mock_get_wf.return_value.return_value = workflow
    mock_get_ch.return_value = MagicMock(name="jira")

    consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
    consumer._run_job(_make_job(agent_view_id=None))

    check_calls = [
        c for c in mock_em.dispatch.call_args_list
        if c.args and c.args[0] == "workspace_build_check_before"
    ]
    assert check_calls == []


@patch("agento.framework.consumer.get_workflow_class")
@patch("agento.framework.consumer.get_channel")
@patch("agento.framework.consumer.create_runner")
@patch("agento.framework.consumer.get_connection")
@patch("agento.framework.consumer.resolve_agent_view_runtime")
@patch("agento.framework.consumer.get_event_manager")
def test_propagates_observer_failure_via_event_error(
    mock_get_em, mock_resolve, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
    sample_db_config, sample_consumer_config,
):
    """Observer captures failure on event.error; consumer re-raises so the job
    fails fast instead of silently running with a stale build."""
    rebuild_exc = RuntimeError("rebuild blew up")

    def fake_dispatch(event_name, event):
        if event_name == "workspace_build_check_before":
            event.error = rebuild_exc

    mock_em = MagicMock()
    mock_em.dispatch.side_effect = fake_dispatch
    mock_get_em.return_value = mock_em

    mock_resolve.return_value = _runtime_with_agent_view()
    mock_conn.return_value = MagicMock()
    mock_get_ch.return_value = MagicMock(name="jira")

    consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
    with pytest.raises(RuntimeError, match="rebuild blew up"):
        consumer._run_job(_make_job(agent_view_id=2))

    MockRunner.assert_not_called()


class TestObserverBehavior:
    """The observer wraps execute_build, captures exceptions, and stores them
    on the event for the consumer to re-raise."""

    def test_observer_calls_execute_build(self):
        from agento.modules.workspace_build.src.observers import BuildFreshnessCheckObserver

        event = WorkspaceBuildCheckEvent(agent_view_id=7)

        with patch(
            "agento.modules.workspace_build.src.observers.get_connection",
        ) as mock_conn, patch(
            "agento.modules.workspace_build.src.observers.DatabaseConfig",
        ) as mock_dbc, patch(
            "agento.modules.workspace_build.src.builder.execute_build",
        ) as mock_execute:
            mock_dbc.from_env.return_value = MagicMock()
            mock_conn.return_value = MagicMock()

            BuildFreshnessCheckObserver().execute(event)

        mock_execute.assert_called_once()
        assert mock_execute.call_args.args[1] == 7
        assert event.error is None

    def test_observer_captures_execute_build_failure(self):
        from agento.modules.workspace_build.src.observers import BuildFreshnessCheckObserver

        event = WorkspaceBuildCheckEvent(agent_view_id=7)
        boom = RuntimeError("rebuild blew up")

        with patch(
            "agento.modules.workspace_build.src.observers.get_connection",
        ) as mock_conn, patch(
            "agento.modules.workspace_build.src.observers.DatabaseConfig",
        ) as mock_dbc, patch(
            "agento.modules.workspace_build.src.builder.execute_build",
            side_effect=boom,
        ):
            mock_dbc.from_env.return_value = MagicMock()
            mock_conn.return_value = MagicMock()

            BuildFreshnessCheckObserver().execute(event)

        assert event.error is boom

    def test_observer_noop_when_event_lacks_agent_view_id(self):
        from agento.modules.workspace_build.src.observers import BuildFreshnessCheckObserver

        event = WorkspaceBuildCheckEvent(agent_view_id=None)  # type: ignore[arg-type]

        with patch(
            "agento.modules.workspace_build.src.builder.execute_build",
        ) as mock_execute:
            BuildFreshnessCheckObserver().execute(event)

        mock_execute.assert_not_called()
