"""Tests for consumer agent_view integration — scoped config, populate_agent_configs, run_dir."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_view_runtime import AgentViewRuntime
from agento.framework.consumer import Consumer
from agento.framework.job_models import AgentType, Job, JobStatus
from agento.framework.workspace import AgentView, Workspace
from agento.modules.claude.src.output_parser import ClaudeResult
from tests.harness_fixtures import stub_workspace_adapters

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


@pytest.fixture(autouse=True)
def _mock_workspace_build_execute():
    """Consumer now dispatches workspace_build_check_before at job-claim time
    as a build-freshness check (see test_consumer_build_freshness.py). The
    observer in workspace_build module calls execute_build under the hood.
    These tests don't seed a real workspace_build row, so mock execute_build
    at the import site used by the observer. Behavior is covered by
    test_consumer_build_freshness.py."""
    with patch("agento.modules.workspace_build.src.builder.execute_build") as m:
        yield m


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=42,
        schedule_id=None,
        type=AgentType.CRON,
        source="jira",
        agent_view_id=None,
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
        idempotency_key="jira:cron:AI-1:20260220_0800",
        status=JobStatus.TODO,
        attempt=0,
        max_attempts=3,
        scheduled_after=datetime(2026, 2, 20, 8, 0),
        started_at=None,
        finished_at=None,
        result_summary=None,
        error_message=None,
        error_class=None,
        pid=None,
        session_id=None,
        created_at=datetime(2026, 2, 20, 7, 59),
        updated_at=datetime(2026, 2, 20, 7, 59),
    )
    defaults.update(overrides)
    return Job(**defaults)


def _make_runtime_with_agent_view(
    agent_view_id: int = 2,
    workspace_id: int = 1,
    harness: str = "claude",
    provider: str = "anthropic",
    model: str = "opus-4",
    scoped_overrides: dict | None = None,
) -> AgentViewRuntime:
    """Create a runtime with a real agent_view + workspace."""
    now = datetime.now(UTC)
    return AgentViewRuntime(
        agent_view=AgentView(
            id=agent_view_id, workspace_id=workspace_id,
            code="developer", label="Developer",
            is_active=True, created_at=now, updated_at=now,
        ),
        workspace=Workspace(
            id=workspace_id, code="acme", label="Acme Corp",
            is_active=True, created_at=now, updated_at=now,
        ),
        harness=harness,
        provider=provider,
        model=model,
        priority=50,
        scoped_overrides=scoped_overrides or {
            "agent_view/mcp/servers": (
                '{"toolbox": {"type": "sse", "url": "http://toolbox:3001/sse"}}',
                False,
            ),
            "agent_view/model": ("opus-4", False),
        },
    )


def _make_claude_result(**overrides) -> ClaudeResult:
    defaults = dict(
        raw_output="ok",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
        num_turns=3,
        duration_ms=5000,
        session_id="success",
        harness="claude",
        prompt=None,
    )
    defaults.update(overrides)
    return ClaudeResult(**defaults)


class TestRunJobWithAgentView:
    """Tests for _run_job when job has agent_view_id set."""

    @pytest.fixture(autouse=True)
    def _mock_token_resolver(self):
        with patch("agento.framework.consumer.CredentialResolver") as MockCls:
            mock_resolver = MagicMock()
            token = MagicMock()
            token.credentials_path = "/etc/tokens/claude_1.json"
            mock_resolver.resolve.return_value = token
            MockCls.return_value = mock_resolver
            yield

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.harness.persistent_home_paths_for", return_value=[])
    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.run_preparation.prepare_artifacts_dir")
    @patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/developer/runs/42")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_calls_config_writer_with_agent_view_id(
        self, mock_resolve, mock_build, mock_prepare, mock_get_writer, mock_persistent,
        mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        runtime = _make_runtime_with_agent_view(agent_view_id=2)
        mock_resolve.return_value = runtime
        mock_conn.return_value = MagicMock()

        mock_writer = MagicMock()
        mock_get_writer.return_value = mock_writer

        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        job = _make_job(agent_view_id=2)

        consumer._run_job(job)

        mock_get_writer.assert_any_call("claude")
        mock_writer.prepare_workspace.assert_called_once()
        call_kwargs = mock_writer.prepare_workspace.call_args
        assert call_kwargs.kwargs["agent_view_id"] == 2

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.harness.persistent_home_paths_for", return_value=[])
    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.run_preparation.prepare_artifacts_dir")
    @patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/developer/runs/42")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_runner_receives_artifacts_dir(
        self, mock_resolve, mock_build, mock_prepare, mock_get_writer, mock_persistent,
        mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        mock_resolve.return_value = _make_runtime_with_agent_view()
        mock_conn.return_value = MagicMock()

        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._run_job(_make_job(agent_view_id=2))

        MockRunner.assert_called_once()
        assert MockRunner.call_args.args[1].working_dir == "/workspace/acme/developer/runs/42"
        assert MockRunner.call_args.args[1].home_dir == "/workspace/acme/developer/runs/42"

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch(
        "agento.framework.consumer.materialize_run_workspace",
        return_value=(
            Path("/workspace/acme/developer/runs/42"),
            Path("/workspace/acme/developer/runs/42"),
        ),
    )
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_materializes_with_resolved_token(
        self, mock_resolve, mock_materialize, mock_conn,
        MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        mock_resolve.return_value = _make_runtime_with_agent_view()
        mock_conn.return_value = MagicMock()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = _make_claude_result()
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._run_job(_make_job(agent_view_id=2))

        mock_materialize.assert_called_once()
        assert (
            mock_materialize.call_args.kwargs["credential"]
            is MockRunner.call_args.args[1].credential
        )
        assert MockRunner.call_args.args[1].home_dir == "/workspace/acme/developer/runs/42"

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_no_agent_view_skips_populate(
        self, mock_resolve, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        """Job with agent_view_id=None still needs an explicit provider; workspace/agent_view are None so artifacts_dir is skipped."""
        runtime = AgentViewRuntime()
        runtime.harness = "claude"
        runtime.provider = "anthropic"
        mock_resolve.return_value = runtime
        mock_conn.return_value = MagicMock()

        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._run_job(_make_job(agent_view_id=None))

        MockRunner.assert_called_once()
        assert MockRunner.call_args.args[1].working_dir == "/workspace"
        assert MockRunner.call_args.args[1].home_dir is None

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_explicit_model_flag_overrides_config(
        self, mock_resolve, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        """An explicit --model flag (Consumer.model_override) wins over runtime.model (config)."""
        runtime = AgentViewRuntime()
        runtime.harness = "claude"
        runtime.provider = "anthropic"
        runtime.model = "db-model"
        mock_resolve.return_value = runtime
        mock_conn.return_value = MagicMock()

        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(
            sample_db_config, sample_consumer_config, logging.getLogger("test"),
            model_override="flag-model",
        )
        consumer._run_job(_make_job(agent_view_id=None))

        MockRunner.assert_called_once()
        assert MockRunner.call_args.args[1].model == "flag-model"

    def test_scoped_overrides_generate_mcp_config_with_agent_view_id(
        self, tmp_path,
    ):
        """End-to-end: ClaudeWorkspaceAdapter writes .mcp.json with agent_view_id in URL."""
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.harness import get_agent_config
        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter

        runtime = _make_runtime_with_agent_view(agent_view_id=5)

        wd = tmp_path / "run"
        wd.mkdir(parents=True)
        with patch(
            "agento.framework.scoped_config.build_scoped_overrides",
            return_value=runtime.scoped_overrides,
        ), patch(
            "agento.framework.scoped_config.load_scoped_db_overrides", return_value={}
        ):
            svc = ScopedConfigService(MagicMock())
        agent_config = get_agent_config(svc)
        writer = ClaudeWorkspaceAdapter()
        writer.prepare_workspace(
            wd, agent_config, agent_view_id=5, toolbox_url="http://toolbox:3001",
        )

        mcp_config = json.loads((wd / ".mcp.json").read_text())
        url = mcp_config["mcpServers"]["toolbox"]["url"]
        assert "agent_view_id=5" in url

        # Also verify .claude.json was generated
        claude_config = json.loads((wd / ".claude.json").read_text())
        assert claude_config["model"] == "opus-4"


class TestRunJobProviderFallback:
    """Tests for provider fallback: agent_view config > primary token."""

    @pytest.fixture(autouse=True)
    def _mock_token_resolver(self):
        with patch("agento.framework.consumer.CredentialResolver") as MockCls:
            mock_resolver = MagicMock()
            token = MagicMock()
            token.credentials_path = "/etc/tokens/codex_1.json"
            mock_resolver.resolve.return_value = token
            MockCls.return_value = mock_resolver
            yield

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.harness.persistent_home_paths_for", return_value=[])
    @patch("agento.framework.harness.workspace_adapter_for")
    @patch("agento.framework.run_preparation.prepare_artifacts_dir")
    @patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/dev/runs/1")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_uses_agent_view_provider_over_primary_token(
        self, mock_resolve, mock_build, mock_prepare, mock_get_writer, mock_persistent,
        mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        mock_resolve.return_value = _make_runtime_with_agent_view(harness="codex", provider="openai", model="o3")
        mock_conn.return_value = MagicMock()

        mock_result = _make_claude_result()
        mock_workflow = MagicMock()
        mock_workflow.execute_job.return_value = mock_result
        mock_get_wf.return_value.return_value = mock_workflow
        mock_get_ch.return_value = MagicMock(name="jira")

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
        consumer._run_job(_make_job(agent_view_id=2))

        MockRunner.assert_called_once()
        assert MockRunner.call_args[0][0] == "codex"
        assert MockRunner.call_args.args[1].model == "o3"

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_raises_when_provider_unset(
        self, mock_resolve, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        """No agent_view/provider configured → raise with actionable message. The
        sticky primary-token fallback is gone; tokens form an LRU pool per provider."""
        mock_resolve.return_value = AgentViewRuntime()  # no provider
        mock_conn.return_value = MagicMock()

        consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))

        with pytest.raises(RuntimeError, match="No agent_view/harness configured"):
            consumer._run_job(_make_job())


class TestPostRunCredentialCapture:
    """Tests that consumer calls capture_refreshed_credentials after execute_job."""

    @pytest.fixture(autouse=True)
    def _mock_token_resolver(self):
        with patch("agento.framework.consumer.CredentialResolver") as MockCls:
            mock_resolver = MagicMock()
            token = MagicMock()
            token.id = 7
            token.credentials = {"raw_auth": {"tokens": {"refresh_token": "old"}}}
            # No lease taken (a MagicMock attribute would otherwise compare equal to
            # nothing and read as truthy), so these runs exercise the unleased path.
            token.lease_owner = None
            token.leased_until = None
            mock_resolver.resolve.return_value = token
            MockCls.return_value = mock_resolver
            yield

    @pytest.fixture(autouse=True)
    def _stub_lifecycle_store_calls(self):
        """The lifecycle also self-heals and releases on the same connection; stub those so
        these tests keep asserting exactly one capture, one commit and one close."""
        with (
            patch("agento.framework.consumer.clear_auto_credential_error") as clear,
            patch("agento.framework.consumer.release_credential_lease") as release,
        ):
            self.clear_auto_credential_error = clear
            self.release_credential_lease = release
            yield

    @patch("agento.framework.run_preparation.copy_build_to_artifacts_dir")
    @patch("agento.framework.run_preparation.get_current_build_dir", return_value=Path("/workspace/acme/developer/current"))
    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.run_preparation.prepare_artifacts_dir")
    @patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/developer/runs/42")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_calls_capture_after_execute_job(
        self, mock_resolve, mock_build, mock_prepare, mock_conn,
        MockRunner, mock_get_ch, mock_get_wf, mock_get_current_build, mock_copy_build,
        sample_db_config, sample_consumer_config,
    ):
        runtime = _make_runtime_with_agent_view(harness="codex", provider="openai")
        mock_resolve.return_value = runtime
        mock_conn.return_value = MagicMock()

        mock_writer = MagicMock(spec=["capture_refreshed_credentials", "prepare_workspace", "owned_paths", "persistent_home_paths", "write_credentials", "inject_runtime_params", "migrate_legacy_workspace_config"])

        with stub_workspace_adapters(codex=mock_writer):
            mock_result = _make_claude_result()
            mock_workflow = MagicMock()
            mock_workflow.execute_job.return_value = mock_result
            mock_get_wf.return_value.return_value = mock_workflow
            mock_get_ch.return_value = MagicMock(name="jira")

            consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
            consumer._run_job(_make_job(agent_view_id=2))

        mock_writer.capture_refreshed_credentials.assert_called_once()
        call_args = mock_writer.capture_refreshed_credentials.call_args
        assert Path(call_args[0][0]) == Path("/workspace/acme/developer/runs/42")

    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_skips_capture_when_no_artifacts_dir(
        self, mock_resolve, mock_conn, MockRunner, mock_get_ch, mock_get_wf,
        sample_db_config, sample_consumer_config,
    ):
        runtime = AgentViewRuntime()
        runtime.harness = "claude"
        runtime.provider = "anthropic"
        mock_resolve.return_value = runtime
        mock_conn.return_value = MagicMock()

        mock_writer = MagicMock()

        with stub_workspace_adapters(claude=mock_writer):
            mock_result = _make_claude_result()
            mock_workflow = MagicMock()
            mock_workflow.execute_job.return_value = mock_result
            mock_get_wf.return_value.return_value = mock_workflow
            mock_get_ch.return_value = MagicMock(name="jira")

            consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
            consumer._run_job(_make_job(agent_view_id=None))

        mock_writer.capture_refreshed_credentials.assert_not_called()

    @patch("agento.framework.run_preparation.copy_build_to_artifacts_dir")
    @patch("agento.framework.run_preparation.get_current_build_dir", return_value=Path("/workspace/acme/developer/current"))
    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.run_preparation.prepare_artifacts_dir")
    @patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/developer/runs/42")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_skips_capture_when_writer_has_no_method(
        self, mock_resolve, mock_build, mock_prepare, mock_conn,
        MockRunner, mock_get_ch, mock_get_wf, mock_get_current_build, mock_copy_build,
        sample_db_config, sample_consumer_config,
    ):
        runtime = _make_runtime_with_agent_view(harness="claude", provider="anthropic")
        mock_resolve.return_value = runtime
        mock_conn.return_value = MagicMock()

        # ClaudeWorkspaceAdapter-like writer without capture_refreshed_credentials
        mock_writer = MagicMock(
            spec=["prepare_workspace", "owned_paths", "persistent_home_paths",
                  "write_credentials"],
        )

        with stub_workspace_adapters(claude=mock_writer):
            mock_result = _make_claude_result()
            mock_workflow = MagicMock()
            mock_workflow.execute_job.return_value = mock_result
            mock_get_wf.return_value.return_value = mock_workflow
            mock_get_ch.return_value = MagicMock(name="jira")

            consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
            # Should not raise
            consumer._run_job(_make_job(agent_view_id=2))

    @patch("agento.framework.run_preparation.copy_build_to_artifacts_dir")
    @patch("agento.framework.run_preparation.get_current_build_dir", return_value=Path("/workspace/acme/developer/current"))
    @patch("agento.framework.consumer.get_workflow_class")
    @patch("agento.framework.consumer.get_channel")
    @patch("agento.framework.consumer.create_runner")
    @patch("agento.framework.consumer.get_connection")
    @patch("agento.framework.run_preparation.prepare_artifacts_dir")
    @patch("agento.framework.run_preparation.build_artifacts_dir", return_value="/workspace/acme/developer/runs/42")
    @patch("agento.framework.consumer.resolve_agent_view_runtime")
    def test_commits_after_successful_capture(
        self, mock_resolve, mock_build, mock_prepare, mock_conn,
        MockRunner, mock_get_ch, mock_get_wf, mock_get_current_build, mock_copy_build,
        sample_db_config, sample_consumer_config,
    ):
        runtime = _make_runtime_with_agent_view(harness="claude", provider="anthropic")
        mock_resolve.return_value = runtime

        # Distinct connection per get_connection() call, so we can assert the
        # exact connection handed to capture() is the one committed and closed.
        conns = []
        def _new_conn(*a, **k):
            c = MagicMock()
            conns.append(c)
            return c
        mock_conn.side_effect = _new_conn

        mock_writer = MagicMock(
            spec=["prepare_workspace", "owned_paths", "persistent_home_paths",
                  "write_credentials", "capture_refreshed_credentials"],
        )

        with stub_workspace_adapters(claude=mock_writer):
            mock_result = _make_claude_result()
            mock_workflow = MagicMock()
            mock_workflow.execute_job.return_value = mock_result
            mock_get_wf.return_value.return_value = mock_workflow
            mock_get_ch.return_value = MagicMock(name="jira")

            consumer = Consumer(sample_db_config, sample_consumer_config, logging.getLogger("test"))
            consumer._run_job(_make_job(agent_view_id=2))

        mock_writer.capture_refreshed_credentials.assert_called_once()
        captured_conn = mock_writer.capture_refreshed_credentials.call_args.args[2]
        captured_conn.commit.assert_called_once()
        captured_conn.close.assert_called_once()
