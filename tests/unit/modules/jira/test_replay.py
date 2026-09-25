"""Tests for src.replay module."""
from __future__ import annotations

from datetime import datetime

import pytest

from agento.framework.harness import clear
from agento.framework.job_models import AgentType, Job, JobStatus
from agento.framework.replay import build_replay_command
from tests.harness_fixtures import register_builtin_harnesses


def _make_job(**overrides) -> Job:
    defaults = dict(
        id=42,
        schedule_id=None,
        type=AgentType.CRON,
        source="jira",
        agent_view_id=None,
        priority=50,
        reference_id="AI-1",
        agent_type="claude",
        provider=None,
        model="claude-sonnet-4-20250514",
        input_tokens=1500,
        output_tokens=800,
        prompt="Zadanie cykliczne (jira) AI-1. Postępuj krok po kroku:",
        output='{"result": "ok"}',
        context=None,
        idempotency_key="jira:cron:AI-1:20260220_0800",
        status=JobStatus.SUCCESS,
        attempt=1,
        max_attempts=3,
        scheduled_after=datetime(2026, 2, 20, 8, 0),
        started_at=datetime(2026, 2, 20, 8, 0, 5),
        finished_at=datetime(2026, 2, 20, 8, 1, 0),
        result_summary="session_id=success turns=3",
        error_message=None,
        error_class=None,
        pid=None,
        session_id=None,
        created_at=datetime(2026, 2, 20, 7, 59),
        updated_at=datetime(2026, 2, 20, 8, 1, 0),
    )
    defaults.update(overrides)
    return Job(**defaults)


@pytest.fixture(autouse=True)
def _harnesses():
    """Register the real shipped harnesses — replay builds its command from the
    harness's own CommandBuilder, so mocking it would assert nothing about parity."""
    register_builtin_harnesses()
    yield
    clear()


class TestBuildReplayCommand:
    def test_claude_command_structure(self):
        job = _make_job(agent_type="claude", model="claude-sonnet-4-20250514")
        rc = build_replay_command(job, harness_config={})

        assert rc.args[0] == "claude"
        assert rc.args[1] == "-p"
        assert rc.args[2] == job.prompt
        assert "--dangerously-skip-permissions" in rc.args
        assert "--output-format" in rc.args
        assert "stream-json" in rc.args
        assert "--model" in rc.args
        assert "claude-sonnet-4-20250514" in rc.args

    def test_claude_command_no_model(self):
        job = _make_job(agent_type="claude", model=None)
        rc = build_replay_command(job, harness_config={})

        assert "--model" not in rc.args
        assert rc.model is None

    def test_codex_command_structure(self):
        job = _make_job(agent_type="codex", model="o3")
        rc = build_replay_command(job, harness_config={})

        assert rc.args[0] == "codex"
        assert rc.args[1] == "exec"
        assert rc.args[2] == job.prompt
        assert "--dangerously-bypass-approvals-and-sandbox" in rc.args
        assert "--model" in rc.args
        assert "o3" in rc.args

    def test_codex_command_no_model(self):
        job = _make_job(agent_type="codex", model=None)
        rc = build_replay_command(job, harness_config={})

        assert "--model" not in rc.args

    def test_no_prompt_raises(self):
        job = _make_job(prompt=None)
        with pytest.raises(ValueError, match="no stored prompt"):
            build_replay_command(job, harness_config={})

    def test_no_agent_type_raises(self):
        job = _make_job(agent_type=None)
        with pytest.raises(ValueError, match="no agent_type"):
            build_replay_command(job, harness_config={})

    def test_unknown_agent_type_raises(self):
        job = _make_job(agent_type="unknown_agent")
        with pytest.raises(ValueError, match="Unknown harness"):
            build_replay_command(job, harness_config={})

    def test_model_override(self):
        job = _make_job(agent_type="claude", model="claude-sonnet-4-20250514")
        rc = build_replay_command(job, model_override="claude-opus-4-20250514", harness_config={})

        assert "claude-opus-4-20250514" in rc.args
        assert "claude-sonnet-4-20250514" not in rc.args
        assert rc.model == "claude-opus-4-20250514"

    def test_agent_type_override_to_codex(self):
        job = _make_job(agent_type="claude", model="claude-sonnet-4-20250514")
        rc = build_replay_command(job, harness_override="codex", model_override="o3", harness_config={})

        assert rc.args[0] == "codex"
        assert rc.harness == "codex"
        assert "o3" in rc.args

    def test_agent_type_override_to_claude(self):
        job = _make_job(agent_type="codex", model="o3")
        rc = build_replay_command(
            job, harness_override="claude", model_override="claude-sonnet-4-20250514",
            harness_config={},
        )

        assert rc.args[0] == "claude"
        assert rc.harness == "claude"
        assert "claude-sonnet-4-20250514" in rc.args

    def test_unknown_agent_type_override_raises(self):
        job = _make_job(agent_type="claude")
        with pytest.raises(ValueError, match="Unknown harness"):
            build_replay_command(job, harness_override="unknown_provider", harness_config={})

    def test_replay_command_metadata(self):
        job = _make_job(agent_type="claude", model="claude-sonnet-4-20250514")
        rc = build_replay_command(job, harness_config={})

        assert rc.harness == "claude"
        assert rc.model == "claude-sonnet-4-20250514"
        assert rc.prompt == job.prompt
        assert rc.job is job

    def test_shell_command_is_safe(self):
        job = _make_job(prompt="prompt with 'single quotes' and spaces")
        rc = build_replay_command(job, harness_config={})
        shell = rc.shell_command

        assert isinstance(shell, str)
        assert "claude" in shell

    def test_shell_command_contains_prompt(self):
        job = _make_job(agent_type="claude", prompt="my test prompt")
        rc = build_replay_command(job, harness_config={})

        assert "my test prompt" in rc.shell_command


class TestReplayCarriesTheHarnessConfig:
    """A replay must reproduce the run — including the sandbox it ran under."""

    def test_sandboxed_codex_job_replays_without_the_bypass_flag(self):
        job = _make_job(agent_type="codex", model="o3", agent_view_id=3)
        rc = build_replay_command(
            job, harness_config={"config": 'sandbox_mode = "workspace-write"\n'},
        )
        assert "--dangerously-bypass-approvals-and-sandbox" not in rc.args
        assert rc.harness_config["config"].startswith("sandbox_mode")

    def test_refuses_when_the_harness_config_cannot_be_resolved(self):
        """Fail closed: a command that may silently differ from the run is worse than none."""
        job = _make_job(agent_type="codex", agent_view_id=None)
        with pytest.raises(ValueError, match="agent_view_id"):
            build_replay_command(job)

    def test_resolution_uses_the_jobs_agent_view_scope_and_closes_the_connection(self, monkeypatch):
        """The one path where replay resolves the config itself, end to end."""
        import agento.framework.config_resolver as config_resolver
        import agento.framework.db as db
        import agento.framework.harness as harness_mod
        import agento.framework.replay as replay_mod
        from agento.framework.scoped_config import Scope

        closed = []
        conn = type("Conn", (), {"close": lambda self: closed.append(True)})()
        seen = {}

        def fake_scoped(c, scope=None, scope_id=None):
            seen["conn"], seen["scope"], seen["scope_id"] = c, scope, scope_id
            return "SVC"

        monkeypatch.setattr(db, "get_connection", lambda cfg: conn)
        monkeypatch.setattr(config_resolver, "ScopedConfigService", fake_scoped)
        monkeypatch.setattr(
            harness_mod, "get_harness_config",
            lambda svc, registered: {"config": 'sandbox_mode = "workspace-write"\n'},
        )
        monkeypatch.setattr(
            replay_mod.DatabaseConfig, "from_env", classmethod(lambda cls: "DBCFG"),
        )

        rc = build_replay_command(_make_job(agent_type="codex", agent_view_id=9))

        assert seen["conn"] is conn
        assert seen["scope"] is Scope.AGENT_VIEW
        assert seen["scope_id"] == 9
        assert closed == [True]
        assert rc.harness_config == {"config": 'sandbox_mode = "workspace-write"\n'}
        assert "--dangerously-bypass-approvals-and-sandbox" not in rc.args
