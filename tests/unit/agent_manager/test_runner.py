from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.errors import AuthenticationError, UsageLimitError
from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.framework.harness import RunRequest, SubprocessRunner
from agento.modules.claude.src.runner import ClaudeSubprocessRunner
from agento.modules.codex.src.runner import CodexSubprocessRunner
from tests.harness_fixtures import make_runner

_EPOCH = datetime(2000, 1, 1)


def _make_token(credentials: dict, agent_type: str = "claude") -> CredentialRecord:
    return CredentialRecord(
        id=1,
        scope=agent_type,
        type="oauth",
        label="test",
        credentials=credentials,
        token_limit=0,
        enabled=True,
        status=CredentialStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )

_CODEX_FIXTURES = Path(__file__).parents[2] / "fixtures" / "codex"


def _make_completed_process(
    returncode: int = 0, stdout: str = "", stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["test"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class TestRunnerProtocolCompliance:
    """Verify that ClaudeSubprocessRunner and CodexSubprocessRunner satisfy the SubprocessRunner Protocol."""

    def test_token_claude_runner_is_runner(self):
        assert issubclass(ClaudeSubprocessRunner, SubprocessRunner) or isinstance(
            make_runner("claude", dry_run=True, credential_required=False), SubprocessRunner
        )

    def test_token_codex_runner_is_runner(self):
        assert issubclass(CodexSubprocessRunner, SubprocessRunner) or isinstance(
            make_runner("codex", dry_run=True, credential_required=False), SubprocessRunner
        )


class TestExtraEnv:
    """extra_env (e.g. GIT_AUTHOR_*/GIT_COMMITTER_*) is merged last so it overrides
    both the credential env and any inherited process env."""

    def test_extra_env_is_merged(self):
        token = _make_token({"raw_auth": {}})  # oauth ⇒ _build_env() == {}
        runner = make_runner("claude",
            dry_run=True, credential=token,
            extra_env={"GIT_AUTHOR_NAME": "Mieszko", "GIT_AUTHOR_EMAIL": "m@example.com"},
        )
        env = {**os.environ, **runner._credential_env(runner.context.credential), **runner.context.extra_env}
        assert env["GIT_AUTHOR_NAME"] == "Mieszko"
        assert env["GIT_AUTHOR_EMAIL"] == "m@example.com"

    def test_extra_env_overrides_inherited_process_env(self, monkeypatch):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Stale Inherited")
        token = _make_token({"raw_auth": {}})
        runner = make_runner("claude",
            dry_run=True, credential=token, extra_env={"GIT_AUTHOR_NAME": "Mieszko"},
        )
        env = {**os.environ, **runner._credential_env(runner.context.credential), **runner.context.extra_env}
        assert env["GIT_AUTHOR_NAME"] == "Mieszko"  # extra_env wins over os.environ

    def test_default_no_extra_env(self):
        token = _make_token({"raw_auth": {}})
        runner = make_runner("claude", dry_run=True, credential=token)
        env = {**os.environ, **runner._credential_env(runner.context.credential), **runner.context.extra_env}
        assert "GIT_AUTHOR_NAME" not in env or env.get("GIT_AUTHOR_NAME") != ""
        assert runner.context.extra_env == {}


class TestTokenRunnerDryRun:
    def test_claude_dry_run(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)

        result = runner.execute(RunRequest(prompt="test prompt"))

        assert result.raw_output == "[DRY RUN] skipped"

    def test_codex_dry_run(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner.execute(RunRequest(prompt="test prompt"))

        assert result.raw_output == "[DRY RUN] skipped"

    def test_claude_resume_dry_run(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        result = runner.execute(RunRequest(prompt='', session_id="session-abc"))
        assert result.raw_output == "[DRY RUN] skipped"

    def test_codex_resume_dry_run(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        result = runner.execute(RunRequest(prompt='', session_id="session-abc"))
        assert result.raw_output == "[DRY RUN] skipped"


class TestClaudeSubprocessRunner:
    def _make_token(self, type_: str, credentials: dict) -> CredentialRecord:
        return CredentialRecord(
            id=1,
            scope="claude",
            type=type_,
            label="test",
            credentials=credentials,
            token_limit=0,
            enabled=True,
            status=CredentialStatus.OK,
            priority=0,
            error_msg=None,
            expires_at=None,
            used_at=None,
            created_at=_EPOCH,
            updated_at=_EPOCH,
        )

    def test_agent_type(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        assert runner.context.harness == "claude"

    def test_build_env_oauth_returns_empty(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        token = self._make_token(
            type_="oauth",
            credentials={"subscription_key": "x", "refresh_token": "y"},
        )
        assert runner._credential_env(token) == {}

    def test_build_env_anthropic_api_key(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        token = self._make_token(
            type_="anthropic_api_key",
            credentials={"api_key": "sk-ant-XYZ"},
        )
        assert runner._credential_env(token) == {"ANTHROPIC_API_KEY": "sk-ant-XYZ"}

    def test_build_env_anthropic_api_key_missing_value_raises(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        token = self._make_token(
            type_="anthropic_api_key",
            credentials={},  # type says api_key but credentials are empty
        )
        with pytest.raises(ValueError, match="anthropic_api_key"):
            runner._credential_env(token)

    def test_build_command(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt="Hello world"))
        assert cmd == [
            "claude", "-p", "Hello world",
            "--dangerously-skip-permissions",
            "--mcp-config", ".mcp.json",
            "--strict-mcp-config",
            "--output-format", "stream-json",
            "--verbose",
        ]

    def test_build_command_with_model(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt="Hello world", model="claude-sonnet-4-20250514"))
        assert cmd == [
            "claude", "-p", "Hello world",
            "--dangerously-skip-permissions",
            "--mcp-config", ".mcp.json",
            "--strict-mcp-config",
            "--output-format", "stream-json",
            "--verbose",
            "--model", "claude-sonnet-4-20250514",
        ]

    def test_build_command_no_model_when_none(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt="Hello", model=None))
        assert "--model" not in cmd

    def test_build_resume_command(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt='', session_id="sess-123"))
        assert cmd == [
            "claude", "--resume", "sess-123",
            "-p", "Continue working from where you left off.",
            "--dangerously-skip-permissions",
            "--mcp-config", ".mcp.json",
            "--strict-mcp-config",
            "--output-format", "stream-json",
            "--verbose",
        ]

    def test_build_resume_command_with_model(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt='', session_id="sess-123", model="claude-sonnet-4-20250514"))
        assert "--model" in cmd
        assert "claude-sonnet-4-20250514" in cmd

    def test_parse_output_valid_json(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        data = {
            "result": "done",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "total_cost_usd": 0.005,
            "num_turns": 1,
            "duration_ms": 2000,
            "subtype": "success",
        }
        result = runner._parse_output(json.dumps(data))
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cost_usd == 0.005

    def test_parse_output_stream_json(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        raw = (
            '{"type": "init", "session_id": "sess-abc"}\n'
            '{"type": "assistant", "message": "hello"}\n'
            '{"type": "result", "result": "done", "usage": {"input_tokens": 200, "output_tokens": 100}, '
            '"total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 3000, "session_id": "sess-abc"}\n'
        )
        result = runner._parse_output(raw)
        assert result.input_tokens == 200
        assert result.output_tokens == 100
        assert result.session_id == "sess-abc"

    def test_parse_output_invalid_json(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        result = runner._parse_output("not json")
        assert result.raw_output == "not json"
        assert result.input_tokens is None

    def test_try_parse_session_id(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        assert runner._try_parse_session_id('{"session_id": "sess-abc"}') == "sess-abc"
        assert runner._try_parse_session_id('{"type": "init"}') is None
        assert runner._try_parse_session_id("not json") is None

    def test_run_executes_subprocess(self, agent_config):
        stream_output = (
            '{"type": "result", "result": "ok", "usage": {"input_tokens": 200, "output_tokens": 100}, '
            '"total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 3000, "session_id": "sess-1"}\n'
        )

        runner = make_runner("claude",

            dry_run=False,
            credential=_make_token({"subscription_key": "sk-ant-test"}),
        )
        runner._record_usage = MagicMock()
        runner._execute_process = MagicMock(
            return_value=_make_completed_process(stdout=stream_output),
        )

        result = runner.execute(RunRequest(prompt="test prompt"))

        assert result.input_tokens == 200
        assert result.output_tokens == 100
        assert result.harness == "claude"
        runner._execute_process.assert_called_once()

    def test_run_raises_when_no_active_token(self, agent_config):
        runner = make_runner("claude",  dry_run=False)
        runner._resolve_token_from_pool = MagicMock(return_value=None)

        with pytest.raises(RuntimeError, match="No healthy credential"):
            runner.execute(RunRequest(prompt="test prompt"))


class TestCodexSubprocessRunner:
    def _make_token(self, type_: str, credentials: dict) -> CredentialRecord:
        return CredentialRecord(
            id=1,
            scope="codex",
            type=type_,
            label="test",
            credentials=credentials,
            token_limit=0,
            enabled=True,
            status=CredentialStatus.OK,
            priority=0,
            error_msg=None,
            expires_at=None,
            used_at=None,
            created_at=_EPOCH,
            updated_at=_EPOCH,
        )

    def test_agent_type(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        assert runner.context.harness == "codex"

    def test_build_env_oauth_returns_empty(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        token = self._make_token(
            type_="oauth",
            credentials={
                "subscription_key": "acc-x",
                "refresh_token": "rt",
                "raw_auth": {"tokens": {"access_token": "acc-x"}},
            },
        )
        assert runner._credential_env(token) == {}

    def test_build_env_openai_api_key_returns_empty(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        token = self._make_token(
            type_="openai_api_key",
            credentials={"api_key": "sk-X"},
        )
        assert runner._credential_env(token) == {}

    def test_build_env_codex_access_token_returns_empty(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        token = self._make_token(
            type_="codex_access_token",
            credentials={"access_token": "eyJ.payload.sig", "expires_at": 9999999999},
        )
        assert runner._credential_env(token) == {}

    def test_build_command(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt="Hello world"))
        assert cmd == [
            "codex", "exec", "Hello world",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]

    def test_build_command_with_model(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt="Hello world", model="o3"))
        assert cmd == [
            "codex", "exec", "Hello world",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--model", "o3",
        ]

    def test_build_resume_command(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt='', session_id="sess-456"))
        assert cmd == [
            "codex", "exec", "resume", "sess-456",
            "Continue working from where you left off.",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]

    def test_build_resume_command_with_model(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt='', session_id="sess-456", model="o3"))
        assert "--model" in cmd
        assert "o3" in cmd

    def test_run_executes_subprocess(self, agent_config):
        runner = make_runner("codex",

            dry_run=False,
            credential=_make_token({"subscription_key": "sk-openai-test"}),
        )
        runner._record_usage = MagicMock()
        stream = (
            '{"type":"thread.started","thread_id":"sess-x"}\n'
            '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"codex result output"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":0}}\n'
        )
        runner._execute_process = MagicMock(
            return_value=_make_completed_process(stdout=stream),
        )

        result = runner.execute(RunRequest(prompt="test prompt"))

        assert "codex result output" in result.raw_output
        assert result.harness == "codex"


class TestCodexSubprocessRunnerJsonOutput:
    """NDJSON-mode parsing (codex exec --json).

    These cover the post-migration contract: stdout carries newline-delimited
    JSON events; auth failure is signalled only by a structured ``turn.failed``
    event whose ``error.message`` matches an anchored auth phrase. The bare
    substring ``"401"`` anywhere in MCP payloads (e.g. order numbers) must NOT
    trigger AuthenticationError — that was the production bug being fixed.
    """

    def test_build_command_includes_json_flag(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt="Hello world"))
        assert "--json" in cmd

    def test_build_resume_command_includes_json_flag(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        cmd = runner.command_builder.headless(runner.context, RunRequest(prompt='', session_id="sess-456"))
        assert "--json" in cmd

    def test_try_parse_session_id_from_thread_started(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        line = '{"type":"thread.started","thread_id":"019e585e-aaa-bbb-ccc"}'
        assert runner._try_parse_session_id(line) == "019e585e-aaa-bbb-ccc"

    def test_try_parse_session_id_ignores_other_events(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        assert runner._try_parse_session_id('{"type":"turn.started"}') is None
        assert runner._try_parse_session_id('{"type":"item.completed","item":{}}') is None

    def test_try_parse_session_id_ignores_non_json(self):
        runner = make_runner("codex", dry_run=True, credential_required=False)
        assert runner._try_parse_session_id("not json") is None
        assert runner._try_parse_session_id("") is None

    def test_parse_output_simple_success(self):
        raw = (_CODEX_FIXTURES / "success_simple.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)

        assert result.session_id == "019e585e-526a-7943-b543-160dddddc56e"
        assert result.input_tokens == 1234
        # output_tokens covers visible reply + reasoning tokens
        assert result.output_tokens == 50
        assert "Hello! I'm ready to help." in result.raw_output

    def test_parse_output_does_NOT_raise_on_substring_401_in_mcp_payload(self):
        """Regression guard for the production token-poisoning bug.

        The string ``401`` appears inside an mcp_tool_call payload (order id
        substring). The legacy text parser scanned the whole blob and
        false-positive'd. The JSON parser must only inspect structured
        ``turn.failed`` events.
        """
        raw = (_CODEX_FIXTURES / "mcp_payload_with_401_substring.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)  # must NOT raise

        assert result.session_id == "019e585e-ab85-7cb1-bdc5-33a5877cb247"
        assert result.input_tokens == 101854
        assert "353043085362789" in result.raw_output

    def test_parse_output_raises_authentication_error_on_turn_failed_401(self):
        raw = (_CODEX_FIXTURES / "auth_failure.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        with pytest.raises(AuthenticationError) as exc_info:
            runner._parse_output(raw)

        assert "401" in str(exc_info.value)

    def test_parse_output_raises_usage_limit_on_turn_failed_429(self):
        raw = (_CODEX_FIXTURES / "usage_limit.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        with pytest.raises(UsageLimitError) as exc_info:
            runner._parse_output(raw)

        assert not isinstance(exc_info.value, AuthenticationError)
        # "try again in 3600 seconds" → a reset time was derived.
        assert exc_info.value.reset_at is not None

    def test_parse_output_does_NOT_raise_on_substring_429_in_mcp_payload(self):
        """Regression guard mirroring the 401 case: a bare '429' inside an MCP
        payload (order id / total) must NOT trigger UsageLimitError — only a
        structured turn.failed.error message does."""
        raw = (_CODEX_FIXTURES / "mcp_payload_with_429_substring.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)  # must NOT raise

        assert "429000112233" in result.raw_output
        assert result.input_tokens == 101854

    def test_parse_output_raises_usage_limit_on_typed_code_with_bland_message(self):
        """A typed limit error whose human message has NO limit phrase must still be
        classified via the structured error.type/code (a codex turn.failed can exit
        rc=0 — missing it would dead-letter as a false SUCCESS)."""
        raw = (
            '{"type":"thread.started","thread_id":"sess-lim"}\n'
            '{"type":"turn.failed","error":{"message":"Request failed.",'
            '"type":"usage_limit_reached","code":"usage_limit_reached"}}\n'
        )
        runner = make_runner("codex", dry_run=True, credential_required=False)
        with pytest.raises(UsageLimitError):
            runner._parse_output(raw)

    def test_parse_output_reset_from_machine_readable_retry_after(self):
        """reset_at is derived from a machine-readable retry_after (seconds) field,
        not only from message text."""
        from datetime import UTC, datetime

        from agento.modules.codex.src.runner import _reset_at_from_error

        base = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
        err = {"message": "rate limit", "retry_after": 3600}
        assert _reset_at_from_error(err, now=base) == datetime(2026, 7, 22, 9, 0)
        # epoch reset field
        epoch = int(datetime(2026, 7, 22, 10, 0, tzinfo=UTC).timestamp())
        assert _reset_at_from_error({"type": "rate_limit", "reset_at": epoch}, now=base) == datetime(
            2026, 7, 22, 10, 0
        )
        # nothing parseable → None
        assert _reset_at_from_error({"type": "rate_limit"}, now=base) is None

    def test_parse_output_no_turn_completed_returns_partial_result(self):
        """If codex dies mid-stream (no turn.completed), parser still extracts
        what's available without raising. CredentialRecord usage is None; raw_output has
        whatever agent_message text was emitted."""
        raw = (_CODEX_FIXTURES / "no_turn_completed.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)

        assert result.session_id == "019e5862-b829-7552-8007-11e34c456a93"
        assert result.input_tokens is None
        assert result.output_tokens is None
        assert "Toolbox not reachable" in result.raw_output

    def test_parse_output_only_thread_started(self):
        """Process killed right after thread.started — we still get the session
        id (so the consumer can resume), no tokens, empty agent text."""
        raw = (_CODEX_FIXTURES / "thread_started_only.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)

        assert result.session_id == "019e5872-aaaa-bbbb-cccc-ddddeeeeffff"
        assert result.input_tokens is None
        assert result.raw_output == ""

    def test_parse_output_skips_malformed_lines(self):
        """Defensive against partial flushes — a non-JSON line in the stream
        must be skipped, not crash the parser."""
        raw = (
            '{"type":"thread.started","thread_id":"sess-x"}\n'
            'GARBAGE LINE NOT JSON\n'
            '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"hi"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":0}}\n'
        )
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)

        assert result.session_id == "sess-x"
        assert result.input_tokens == 10
        assert result.output_tokens == 2
        assert "hi" in result.raw_output

    def test_parse_output_real_success_with_mcp_calls(self):
        """Real captured sample — order lookup w/ multiple toolbox MCP calls."""
        raw = (_CODEX_FIXTURES / "real_success_with_mcp.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)

        assert result.session_id == "019e585e-ab85-7cb1-bdc5-33a5877cb247"
        # turn.completed.usage.input_tokens
        assert result.input_tokens == 101854
        # output_tokens (1904) + reasoning_output_tokens (833)
        assert result.output_tokens == 1904 + 833
        # raw_output is the concatenated agent_message text(s) from item.completed
        assert "353043085362789" in result.raw_output
        # codex emits no session-level MCP init self-report (only per-call
        # mcp_tool_call items), so the connection signal stays unknown.
        assert result.mcp_init is None

    def test_codex_populate_mcp_init_returns_none_when_absent(self):
        """Empirical: ``codex exec --json`` (through 0.128.0) emits no
        session-level MCP-server init self-report — MCP only appears as
        per-call ``mcp_tool_call`` items, which report invocation, not
        startup connection status. ``_populate_mcp_init`` therefore leaves
        ``result.mcp_init = None`` ("we don't know"). See the README note in
        ``app_monitor`` and the docstring on ``_populate_mcp_init``. If a
        future codex version ships a real init event, add a
        ``with_mcp_init.ndjson`` fixture and a positive test here."""
        raw = (_CODEX_FIXTURES / "real_success_with_mcp.ndjson").read_text()
        runner = make_runner("codex", dry_run=True, credential_required=False)

        result = runner._parse_output(raw)

        assert result.mcp_init is None

    def test_extract_raw_uses_stdout_only_not_stderr(self):
        """The new parser MUST NOT concatenate stderr. Codex log lines on
        stderr (Rust tracing output) contain '401' substrings that would
        false-positive substring-based auth detection — we sidestep that
        entirely by ignoring stderr."""
        runner = make_runner("codex", dry_run=True, credential_required=False)
        proc = _make_completed_process(
            stdout='{"type":"thread.started","thread_id":"s"}\n',
            stderr="ERROR codex_api: HTTP error: 401 Unauthorized\n",
        )

        raw = runner._extract_raw(proc)

        assert "401" not in raw
        assert "thread.started" in raw


class TestSubprocessTimeout:
    def test_timeout_passed_to_init(self):
        runner = make_runner("claude", dry_run=True, timeout_seconds=900)
        assert runner.context.timeout_seconds == 900

    def test_default_timeout(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        assert runner.context.timeout_seconds == 1200

    def test_timeout_expired_propagates(self, agent_config):
        exc = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        exc.session_id = None  # type: ignore[attr-defined]

        runner = make_runner("claude",
             dry_run=False, timeout_seconds=600,
            credential=_make_token({"subscription_key": "sk-ant-test"}),
        )
        runner._execute_process = MagicMock(side_effect=exc)

        with pytest.raises(subprocess.TimeoutExpired):
            runner.execute(RunRequest(prompt="test"))

    def test_timeout_with_session_id(self, agent_config):
        exc = subprocess.TimeoutExpired(cmd="claude", timeout=600)
        exc.session_id = "sess-timeout-abc"  # type: ignore[attr-defined]

        runner = make_runner("claude",
             dry_run=False, timeout_seconds=600,
            credential=_make_token({"subscription_key": "sk-ant-test"}),
        )
        runner._execute_process = MagicMock(side_effect=exc)

        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            runner.execute(RunRequest(prompt="test"))

        assert exc_info.value.session_id == "sess-timeout-abc"  # type: ignore[attr-defined]


class TestCredentialClaimedByCaller:
    """The caller claims the credential; the runner never reaches the pool itself.

    Splitting the claim out of the runner is what guarantees the command and the
    spawned process use the SAME credential — the old runner-side pool fallback
    could hand the env one credential and the built command another.
    """

    def test_uses_the_context_credential_verbatim(self, agent_config):
        from .conftest import make_token

        token = make_token(
            id=10,
            agent_type="claude",
            type="anthropic_api_key",
            credentials={"api_key": "sk-ant-X"},
        )
        stream = '{"type": "result", "result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}\n'

        runner = make_runner("claude", dry_run=False, credential=token)
        runner._record_usage = MagicMock()
        captured_env = {}

        def _fake_execute(_cmd, env, stdin_payload=None):
            captured_env.update(env)
            return _make_completed_process(stdout=stream)

        runner._execute_process = MagicMock(side_effect=_fake_execute)

        runner.execute(RunRequest(prompt="test"))

        assert captured_env["ANTHROPIC_API_KEY"] == "sk-ant-X"
        assert runner.context.credential is token

    def test_runner_has_no_pool_access(self):
        runner = make_runner("claude", dry_run=True)
        for name in ("_resolve_token_from_pool", "_resolve_credential_from_pool"):
            assert not hasattr(runner, name)

    def test_missing_required_credential_raises(self, agent_config):
        runner = make_runner("claude", dry_run=False, credential=None)
        runner._execute_process = MagicMock()

        with pytest.raises(RuntimeError, match="No healthy credential"):
            runner.execute(RunRequest(prompt="test"))

        runner._execute_process.assert_not_called()


class TestRecordUsageBestEffort:
    """Verify that usage recording failures don't crash the runner."""

    def test_continues_on_usage_recording_failure(self, agent_config):
        stream_output = '{"type": "result", "result": "ok", "usage": {"input_tokens": 10, "output_tokens": 5}}\n'

        runner = make_runner("claude",

            dry_run=False,
            credential=_make_token({"subscription_key": "sk-test"}),
        )
        runner._execute_process = MagicMock(
            return_value=_make_completed_process(stdout=stream_output),
        )

        # _record_usage silently swallows errors (no DB in test env) — run() should still return
        result = runner.execute(RunRequest(prompt="test"))

        assert result.input_tokens == 10
        assert result.output_tokens == 5


class TestPidAndSessionCallbacks:
    """Verify PID and session_id callbacks are invoked during _execute_process."""

    def test_pid_callback_invoked(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        pids = []
        runner.pid_callback = lambda pid: pids.append(pid)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = iter([])
        mock_proc.stderr = iter([])
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with patch("agento.framework.harness.subprocess_runner.subprocess.Popen", return_value=mock_proc):
            runner._execute_process(["echo", "test"], {})

        assert pids == [12345]

    def test_session_id_callback_invoked(self):
        runner = make_runner("claude", dry_run=True, credential_required=False)
        session_ids = []
        runner.session_id_callback = lambda sid: session_ids.append(sid)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout = iter(['{"session_id": "sess-abc"}\n', '{"type": "result"}\n'])
        mock_proc.stderr = iter([])
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0

        with patch("agento.framework.harness.subprocess_runner.subprocess.Popen", return_value=mock_proc):
            runner._execute_process(["echo", "test"], {})

        assert session_ids == ["sess-abc"]


class TestResumeMethod:
    """Verify resume() calls _build_resume_command and delegates to _execute_and_parse."""

    def test_resume_calls_resume_command(self, agent_config):
        stream_output = (
            '{"type": "result", "result": "ok", "usage": {"input_tokens": 50, "output_tokens": 30}, '
            '"session_id": "sess-resumed"}\n'
        )

        runner = make_runner("claude",

            dry_run=False,
            credential=_make_token({"subscription_key": "sk-ant-test"}),
        )
        runner._record_usage = MagicMock()
        runner._execute_process = MagicMock(
            return_value=_make_completed_process(stdout=stream_output),
        )

        result = runner.execute(RunRequest(prompt='', session_id="sess-original"))

        assert result.input_tokens == 50
        assert result.harness == "claude"

        # Verify the command passed to _execute_process contains --resume
        call_args = runner._execute_process.call_args[0][0]
        assert "--resume" in call_args
        assert "sess-original" in call_args
