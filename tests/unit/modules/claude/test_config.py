"""Tests for ClaudeConfigWriter — .claude.json, .claude/settings.json, .mcp.json."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.agent_manager.models import AgentProvider, Token, TokenStatus
from agento.modules.claude.src.config import ClaudeConfigWriter

_EPOCH = datetime(2000, 1, 1)


def _make_token(credentials: dict, type_: str = "oauth") -> Token:
    return Token(
        id=1,
        agent_type=AgentProvider.CLAUDE,
        type=type_,
        label="test",
        credentials=credentials,
        token_limit=0,
        enabled=True,
        status=TokenStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )


@pytest.fixture
def writer():
    return ClaudeConfigWriter()


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "workspace" / "main" / "alpha"
    d.mkdir(parents=True)
    return d


class TestPrepareWorkspace:
    TOOLBOX = "http://toolbox:3001"

    def test_generates_claude_json_with_model(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {"model": "opus-4"}, toolbox_url=self.TOOLBOX)
        data = json.loads((work_dir / ".claude.json").read_text())
        assert data["model"] == "opus-4"

    def test_generates_system_prompt(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"model": "sonnet", "claude/personality": "Be concise."},
            toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".claude.json").read_text())
        assert data["systemPrompt"] == "Be concise."

    def test_generates_permissions(self, writer, work_dir):
        perms = '{"allow": ["Read", "Write"]}'
        writer.prepare_workspace(
            work_dir, {"claude/permissions": perms}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".claude.json").read_text())
        assert data["permissions"] == {"allow": ["Read", "Write"]}

    def test_skips_invalid_permissions_json(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"model": "opus", "claude/permissions": "not-json{"},
            toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".claude.json").read_text())
        assert "permissions" not in data

    def test_generates_settings_json_full_trust(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"claude/trust_level": "full"}, toolbox_url=self.TOOLBOX,
        )
        settings_path = work_dir / ".claude" / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert data["permissions"]["dangerouslySkipPermissions"] is True

    def test_trust_level_not_full(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"claude/trust_level": "limited"}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".claude" / "settings.json").read_text())
        assert data["permissions"]["dangerouslySkipPermissions"] is False

    def test_empty_config_still_writes_toolbox_mcp(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {}, toolbox_url=self.TOOLBOX)
        assert not (work_dir / ".claude.json").exists()
        assert not (work_dir / ".claude" / "settings.json").exists()
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"]["url"] == "http://toolbox:3001/mcp"

    def test_toolbox_entry_has_explicit_type(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {}, toolbox_url=self.TOOLBOX)
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"] == {
            "type": "http",
            "url": "http://toolbox:3001/mcp",
        }

    def test_every_generated_server_entry_has_a_type(self, writer, work_dir):
        servers = '{"other": {"url": "http://other:4000/sse"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]
        for name, cfg in data["mcpServers"].items():
            assert "type" in cfg, f"{name} has no type"

    def test_derives_sse_for_sse_extra(self, writer, work_dir):
        servers = '{"other": {"url": "http://other:4000/sse"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["other"]["type"] == "sse"

    def test_derives_stdio_for_command_extra(self, writer, work_dir):
        servers = '{"local": {"command": "npx", "args": ["-y", "server"]}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["local"]["type"] == "stdio"
        assert "url" not in data["mcpServers"]["local"]

    def test_defaults_to_http_for_ambiguous_url(self, writer, work_dir):
        servers = '{"other": {"url": "https://example.com/api"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["other"]["type"] == "http"

    def test_malformed_url_falls_back_to_http(self, writer, work_dir):
        servers = '{"other": {"url": "http://[::1"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["other"]["type"] == "http"

    def test_does_not_log_mcp_url_secrets(self, writer, work_dir, caplog):
        servers = '{"other": {"url": "https://user:sup3rsecret@example.com/api?key=t0ken"}}'
        with caplog.at_level("WARNING"):
            writer.prepare_workspace(
                work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
            )
        assert "sup3rsecret" not in caplog.text
        assert "t0ken" not in caplog.text
        assert "example.com" not in caplog.text
        assert "other" in caplog.text

    def test_explicit_operator_type_is_preserved(self, writer, work_dir):
        servers = '{"toolbox": {"type": "sse", "url": "http://toolbox:3001/sse"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"] == {
            "type": "sse",
            "url": "http://toolbox:3001/sse",
        }

    def test_drops_untypeable_extra(self, writer, work_dir):
        servers = '{"weird": {"foo": 1}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert list(data["mcpServers"].keys()) == ["toolbox"]

    def test_non_dict_extra_is_dropped_and_toolbox_survives(self, writer, work_dir):
        servers = '{"toolbox": "nope", "weird": 1}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers},
            agent_view_id=3, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"] == {
            "toolbox": {
                "type": "http",
                "url": "http://toolbox:3001/mcp?agent_view_id=3",
            },
        }

    def test_extras_merge_with_toolbox_mcp(self, writer, work_dir):
        extras = '{"other": {"command": "npx", "args": ["-y", "server"]}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": extras}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert set(data["mcpServers"].keys()) == {"toolbox", "other"}

    def test_always_writes_toolbox_when_no_extras(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {"model": "opus"}, toolbox_url=self.TOOLBOX)
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"]["url"] == "http://toolbox:3001/mcp"

    def test_ignores_invalid_extras_json(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {"mcp/servers": "not-json{"}, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert list(data["mcpServers"].keys()) == ["toolbox"]

    def test_appends_agent_view_id_to_toolbox_url(self, writer, work_dir):
        writer.prepare_workspace(
            work_dir, {}, agent_view_id=2, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"]["url"] == "http://toolbox:3001/mcp?agent_view_id=2"

    def test_no_agent_view_id_leaves_url_unchanged(self, writer, work_dir):
        writer.prepare_workspace(work_dir, {}, toolbox_url=self.TOOLBOX)
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"]["url"] == "http://toolbox:3001/mcp"

    def test_does_not_modify_non_mcp_urls(self, writer, work_dir):
        servers = '{"other": {"type": "stdio", "command": "node"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers},
            agent_view_id=5, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert "url" not in data["mcpServers"]["other"]

    def test_injects_into_any_sse_or_mcp_url(self, writer, work_dir):
        servers = '{"other": {"type": "sse", "url": "http://other:4000/sse"}}'
        writer.prepare_workspace(
            work_dir, {"mcp/servers": servers},
            agent_view_id=5, toolbox_url=self.TOOLBOX,
        )
        data = json.loads((work_dir / ".mcp.json").read_text())
        assert "agent_view_id=5" in data["mcpServers"]["other"]["url"]


class TestInjectRuntimeParams:
    def test_appends_params_to_mcp_json(self, writer, work_dir):
        mcp = {"mcpServers": {"toolbox": {"url": "http://toolbox:3001/sse?agent_view_id=2"}}}
        (work_dir / ".mcp.json").write_text(json.dumps(mcp))

        writer.inject_runtime_params(work_dir, job_id=10)

        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"]["url"] == (
            "http://toolbox:3001/sse?agent_view_id=2&job_id=10"
        )

    def test_noop_when_no_mcp_json(self, writer, work_dir):
        writer.inject_runtime_params(work_dir, job_id=10)
        assert not (work_dir / ".mcp.json").exists()

    def test_handles_mcp_url(self, writer, work_dir):
        mcp = {"mcpServers": {"toolbox": {"url": "http://toolbox:3001/mcp?agent_view_id=2"}}}
        (work_dir / ".mcp.json").write_text(json.dumps(mcp))

        writer.inject_runtime_params(work_dir, job_id=5)

        data = json.loads((work_dir / ".mcp.json").read_text())
        assert "job_id=5" in data["mcpServers"]["toolbox"]["url"]

    def test_preserves_type(self, writer, work_dir):
        mcp = {
            "mcpServers": {
                "toolbox": {
                    "type": "http",
                    "url": "http://toolbox:3001/mcp?agent_view_id=1",
                },
            },
        }
        (work_dir / ".mcp.json").write_text(json.dumps(mcp))

        writer.inject_runtime_params(work_dir, job_id=7)

        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["toolbox"]["type"] == "http"
        assert "job_id=7" in data["mcpServers"]["toolbox"]["url"]

    def test_tolerates_malformed_entries(self, writer, work_dir):
        mcp = {
            "mcpServers": {
                "broken": "nope",
                "numeric": {"type": "http", "url": 123},
                "toolbox": {"type": "http", "url": "http://toolbox:3001/mcp"},
            },
        }
        (work_dir / ".mcp.json").write_text(json.dumps(mcp))

        writer.inject_runtime_params(work_dir, job_id=8)

        data = json.loads((work_dir / ".mcp.json").read_text())
        assert data["mcpServers"]["broken"] == "nope"
        assert data["mcpServers"]["numeric"]["url"] == 123
        assert "job_id=8" in data["mcpServers"]["toolbox"]["url"]


class TestWriteCredentials:
    def test_writes_credentials_json_in_claude_ai_oauth_format(self, writer, work_dir):
        creds = {
            "subscription_key": "sk-ant-oat01-xyz",
            "refresh_token": "rt-123",
            "expires_at": 1799999999,
            "subscription_type": "team",
            "id_token": "ignored",
            "raw_auth": "ignored",
        }
        writer.write_credentials(work_dir, _make_token(creds))

        path = work_dir / ".claude" / ".credentials.json"
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data == {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-xyz",
                "refreshToken": "rt-123",
                "expiresAt": 1799999999,
                "subscriptionType": "team",
            }
        }
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_skips_when_no_subscription_key(self, writer, work_dir):
        writer.write_credentials(work_dir, _make_token({"refresh_token": "x"}))
        assert not (work_dir / ".claude" / ".credentials.json").exists()

    def test_degenerate_oauth_clears_copied_oauth_state(self, writer, work_dir):
        (work_dir / ".claude").mkdir(parents=True)
        (work_dir / ".claude" / ".credentials.json").write_text("stale")
        backups = work_dir / ".claude" / "backups"
        backups.mkdir()
        stale_backup = backups / ".claude.json.backup.1780206566242"
        stale_backup.write_text("stale")
        (work_dir / ".claude.json").write_text(json.dumps({
            "model": "opus-4-7",
            "oauthAccount": {"emailAddress": "old@example.com"},
            "userID": "old-user",
        }))

        writer.write_credentials(work_dir, _make_token({"refresh_token": "x"}))

        assert not (work_dir / ".claude" / ".credentials.json").exists()
        assert not stale_backup.exists()
        out = json.loads((work_dir / ".claude.json").read_text())
        assert out == {"model": "opus-4-7"}

    def test_optional_fields_become_null(self, writer, work_dir):
        writer.write_credentials(work_dir, _make_token({"subscription_key": "sk-x"}))
        data = json.loads((work_dir / ".claude" / ".credentials.json").read_text())
        oauth = data["claudeAiOauth"]
        assert oauth["accessToken"] == "sk-x"
        assert oauth["refreshToken"] is None
        assert oauth["expiresAt"] is None
        assert oauth["subscriptionType"] is None

    def test_preserves_scopes_and_rate_limit_tier_from_raw_auth(self, writer, work_dir):
        raw_creds = {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-abc",
                "refreshToken": "sk-ant-ort01-def",
                "expiresAt": 1776946615316,
                "scopes": [
                    "user:file_upload",
                    "user:inference",
                    "user:mcp_servers",
                    "user:profile",
                    "user:sessions:claude_code",
                ],
                "subscriptionType": "team",
                "rateLimitTier": "default_claude_max_5x",
            }
        }
        creds = {
            "subscription_key": "sk-ant-oat01-abc",
            "refresh_token": "sk-ant-ort01-def",
            "expires_at": 1776946615316,
            "subscription_type": "team",
            "raw_auth": {"credentials": raw_creds, "claude_json": {}},
        }
        writer.write_credentials(work_dir, _make_token(creds))

        data = json.loads((work_dir / ".claude" / ".credentials.json").read_text())
        assert data == raw_creds

    def test_writes_claude_json_with_oauth_account(self, writer, work_dir):
        claude_json = {
            "oauthAccount": {
                "emailAddress": "user@company.com",
                "organizationName": "My company",
            },
            "numStartups": 3,
            "userID": "abc123",
        }
        creds = {
            "subscription_key": "sk-x",
            "raw_auth": {
                "credentials": {"claudeAiOauth": {"accessToken": "sk-x"}},
                "claude_json": claude_json,
            },
        }
        writer.write_credentials(work_dir, _make_token(creds))

        out = json.loads((work_dir / ".claude.json").read_text())
        assert out["oauthAccount"]["emailAddress"] == "user@company.com"
        assert out["numStartups"] == 3
        assert out["userID"] == "abc123"

    def test_merges_claude_json_with_existing_workspace_config(self, writer, work_dir):
        # prepare_workspace has already run and wrote agent_view-level config
        (work_dir / ".claude.json").write_text(json.dumps({
            "model": "opus-4-7",
            "systemPrompt": "Be concise.",
            "permissions": {"allow": ["Read"]},
        }))

        creds = {
            "subscription_key": "sk-x",
            "raw_auth": {
                "credentials": {"claudeAiOauth": {"accessToken": "sk-x"}},
                "claude_json": {
                    "oauthAccount": {"emailAddress": "m@k.com"},
                    "userID": "abc",
                },
            },
        }
        writer.write_credentials(work_dir, _make_token(creds))

        out = json.loads((work_dir / ".claude.json").read_text())
        # agent_view config survives
        assert out["model"] == "opus-4-7"
        assert out["systemPrompt"] == "Be concise."
        assert out["permissions"] == {"allow": ["Read"]}
        # oauth state added
        assert out["oauthAccount"] == {"emailAddress": "m@k.com"}
        assert out["userID"] == "abc"

    def test_auth_state_wins_over_stale_build_claude_json(self, writer, work_dir):
        # Simulate Claude having written stale first-run state in a prior build
        (work_dir / ".claude.json").write_text(json.dumps({
            "userID": "old-fingerprint",
            "numStartups": 1,
        }))

        creds = {
            "subscription_key": "sk-x",
            "raw_auth": {
                "credentials": {"claudeAiOauth": {"accessToken": "sk-x"}},
                "claude_json": {
                    "userID": "new-fingerprint",
                    "numStartups": 5,
                    "oauthAccount": {"emailAddress": "m@k.com"},
                },
            },
        }
        writer.write_credentials(work_dir, _make_token(creds))

        out = json.loads((work_dir / ".claude.json").read_text())
        assert out["userID"] == "new-fingerprint"
        assert out["numStartups"] == 5
        assert out["oauthAccount"] == {"emailAddress": "m@k.com"}

    def test_skips_claude_json_when_raw_auth_missing(self, writer, work_dir):
        writer.write_credentials(work_dir, _make_token({"subscription_key": "sk-x"}))
        assert not (work_dir / ".claude.json").exists()

    def test_legacy_oauth_strips_stale_claude_identity(self, writer, work_dir):
        (work_dir / ".claude.json").write_text(json.dumps({
            "model": "opus-4-7",
            "oauthAccount": {"emailAddress": "old@example.com"},
            "userID": "old-user",
        }))

        writer.write_credentials(work_dir, _make_token({"subscription_key": "sk-x"}))

        assert (work_dir / ".claude" / ".credentials.json").is_file()
        out = json.loads((work_dir / ".claude.json").read_text())
        assert out == {"model": "opus-4-7"}

    def test_skips_claude_json_when_claude_json_empty(self, writer, work_dir):
        creds = {
            "subscription_key": "sk-x",
            "raw_auth": {
                "credentials": {"claudeAiOauth": {"accessToken": "sk-x"}},
                "claude_json": {},
            },
        }
        writer.write_credentials(work_dir, _make_token(creds))
        assert not (work_dir / ".claude.json").exists()

    def test_non_dict_raw_auth_falls_back_to_legacy_fields(self, writer, work_dir):
        # Old rows stored before raw_auth capture: raw_auth may be a string or None.
        creds = {
            "subscription_key": "sk-legacy",
            "refresh_token": "rt-legacy",
            "expires_at": 1799999999,
            "subscription_type": "team",
            "raw_auth": "ignored-string",
        }
        writer.write_credentials(work_dir, _make_token(creds))

        data = json.loads((work_dir / ".claude" / ".credentials.json").read_text())
        assert data == {
            "claudeAiOauth": {
                "accessToken": "sk-legacy",
                "refreshToken": "rt-legacy",
                "expiresAt": 1799999999,
                "subscriptionType": "team",
            }
        }
        assert not (work_dir / ".claude.json").exists()

    def test_does_not_leak_projects_per_cwd_state(self, writer, work_dir):
        # prepare_workspace wrote agent_view config; ``_write_mcp_json`` wrote
        # toolbox into .mcp.json. The captured developer ~/.claude.json carries
        # a per-CWD ``projects`` entry whose ``enabledMcpjsonServers: []`` would
        # silently override the toolbox enablement if leaked into build_dir.
        (work_dir / ".claude.json").write_text(json.dumps({
            "model": "opus-4-7",
            "systemPrompt": "Be concise.",
        }))

        creds = {
            "subscription_key": "sk-x",
            "raw_auth": {
                "credentials": {"claudeAiOauth": {"accessToken": "sk-x"}},
                "claude_json": {
                    "oauthAccount": {"emailAddress": "m@k.com"},
                    "userID": "abc",
                    "projects": {
                        "/workspace": {
                            "enabledMcpjsonServers": [],
                            "hasTrustDialogAccepted": True,
                            "lastSessionId": "leaked-id",
                        },
                        "/Users/dev/elsewhere": {"enabledMcpjsonServers": ["x"]},
                    },
                },
            },
        }
        writer.write_credentials(work_dir, _make_token(creds))

        out = json.loads((work_dir / ".claude.json").read_text())
        # Auth identity copied through
        assert out["oauthAccount"] == {"emailAddress": "m@k.com"}
        assert out["userID"] == "abc"
        # Agent_view config from prepare_workspace survives
        assert out["model"] == "opus-4-7"
        assert out["systemPrompt"] == "Be concise."
        # Per-CWD developer state did NOT leak
        assert "projects" not in out

    def test_payload_with_only_non_identity_keys_leaves_build_config_intact(
        self, writer, work_dir,
    ):
        # If the captured payload has no auth-identity keys (only leaky state),
        # the build's existing .claude.json must be left untouched — we must
        # not write an empty merged file over the prepare_workspace output.
        (work_dir / ".claude.json").write_text(json.dumps({
            "model": "opus-4-7",
            "systemPrompt": "Be concise.",
        }))

        creds = {
            "subscription_key": "sk-x",
            "raw_auth": {
                "credentials": {"claudeAiOauth": {"accessToken": "sk-x"}},
                "claude_json": {
                    "projects": {"/workspace": {"enabledMcpjsonServers": []}},
                    "telemetryNoise": "ignored",
                },
            },
        }
        writer.write_credentials(work_dir, _make_token(creds))

        out = json.loads((work_dir / ".claude.json").read_text())
        assert out == {"model": "opus-4-7", "systemPrompt": "Be concise."}

    def test_api_key_strips_stale_oauth_state_and_preserves_config(self, writer, work_dir):
        (work_dir / ".claude").mkdir(parents=True)
        (work_dir / ".claude" / ".credentials.json").write_text("oauth")
        (work_dir / ".claude.json").write_text(json.dumps({
            "model": "opus-4-7",
            "systemPrompt": "Be concise.",
            "permissions": {"allow": ["Read"]},
            "oauthAccount": {"emailAddress": "old@example.com"},
            "userID": "old-user",
            "numStartups": 9,
            "firstStartTime": "old",
            "hasCompletedOnboarding": True,
        }))

        writer.write_credentials(
            work_dir,
            _make_token({"api_key": "sk-ant-api"}, type_="anthropic_api_key"),
        )

        assert not (work_dir / ".claude" / ".credentials.json").exists()
        out = json.loads((work_dir / ".claude.json").read_text())
        assert out == {
            "model": "opus-4-7",
            "systemPrompt": "Be concise.",
            "permissions": {"allow": ["Read"]},
        }

    def test_api_key_deletes_claude_json_when_only_auth_state_remains(self, writer, work_dir):
        (work_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "old@example.com"},
            "userID": "old-user",
        }))

        writer.write_credentials(
            work_dir,
            _make_token({"api_key": "sk-ant-api"}, type_="anthropic_api_key"),
        )

        assert not (work_dir / ".claude.json").exists()

    @pytest.mark.parametrize("payload", ["not-json{", "[1, 2, 3]"])
    def test_api_key_deletes_malformed_or_non_dict_claude_json(
        self, writer, work_dir, payload,
    ):
        (work_dir / ".claude.json").write_text(payload)

        writer.write_credentials(
            work_dir,
            _make_token({"api_key": "sk-ant-api"}, type_="anthropic_api_key"),
        )

        assert not (work_dir / ".claude.json").exists()

    def test_api_key_removes_claude_json_backups(self, writer, work_dir):
        backups = work_dir / ".claude" / "backups"
        backups.mkdir(parents=True)
        stale_backup = backups / ".claude.json.backup.1780206566242"
        stale_backup.write_text("oauth")
        other_backup = backups / "settings.json.backup.1780206566242"
        other_backup.write_text("keep")

        writer.write_credentials(
            work_dir,
            _make_token({"api_key": "sk-ant-api"}, type_="anthropic_api_key"),
        )

        assert not stale_backup.exists()
        assert other_backup.exists()

    def test_api_key_missing_secret_raises(self, writer, work_dir):
        with pytest.raises(ValueError, match="anthropic_api_key"):
            writer.write_credentials(
                work_dir,
                _make_token({}, type_="anthropic_api_key"),
            )


class TestOwnedPaths:
    def test_returns_claude_files_and_dir(self, writer):
        files, dirs = writer.owned_paths()
        assert files == {".claude.json", ".mcp.json"}
        assert dirs == {".claude"}


class TestMigrateLegacyWorkspaceConfig:
    def test_merges_enabled_mcp_servers_from_legacy_settings_local(self, writer, work_dir, tmp_path):
        build_settings_dir = work_dir / ".claude"
        build_settings_dir.mkdir(parents=True)
        (build_settings_dir / "settings.local.json").write_text(
            json.dumps({"enabledMcpjsonServers": ["other"]})
        )

        workspace_root = tmp_path / "workspace"
        legacy_settings_dir = workspace_root / ".claude"
        legacy_settings_dir.mkdir(parents=True)
        (legacy_settings_dir / "settings.local.json").write_text(
            json.dumps({"enabledMcpjsonServers": ["toolbox"]})
        )

        writer.migrate_legacy_workspace_config(work_dir, workspace_root)

        data = json.loads((build_settings_dir / "settings.local.json").read_text())
        assert data["enabledMcpjsonServers"] == ["toolbox", "other"]


class TestCredentialEnv:
    """Provider-specific credential→env mapping (used by TokenRunner._build_env
    and by the host-side `agento run` env injection path).
    """

    def _typed_token(self, type_: str, credentials: dict) -> Token:
        return Token(
            id=1, agent_type=AgentProvider.CLAUDE, type=type_, label="test",
            credentials=credentials, token_limit=0, enabled=True,
            status=TokenStatus.OK, priority=0, error_msg=None,
            expires_at=None, used_at=None,
            created_at=_EPOCH, updated_at=_EPOCH,
        )

    def test_oauth_returns_empty(self, writer):
        token = self._typed_token("oauth", {"subscription_key": "x", "refresh_token": "y"})
        assert writer.credential_env(token) == {}

    def test_anthropic_api_key_returns_env(self, writer):
        token = self._typed_token("anthropic_api_key", {"api_key": "sk-ant-XYZ"})
        assert writer.credential_env(token) == {"ANTHROPIC_API_KEY": "sk-ant-XYZ"}

    def test_anthropic_api_key_missing_raises(self, writer):
        token = self._typed_token("anthropic_api_key", {})
        with pytest.raises(ValueError, match="anthropic_api_key"):
            writer.credential_env(token)


class TestCaptureRefreshedCredentials:
    """ClaudeConfigWriter persists CLI-rotated .claude/.credentials.json back to the DB."""

    def _oauth_token(self, refresh="rt-OLD", access="acc-OLD"):
        raw_creds = {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": 1776946615316,
                "subscriptionType": "team",
            }
        }
        creds = {
            "subscription_key": access,
            "refresh_token": refresh,
            "expires_at": 1776946615316,
            "subscription_type": "team",
            "raw_auth": {"credentials": raw_creds, "claude_json": {"oauthAccount": {"emailAddress": "user@example.test"}}},
        }
        return _make_token(creds, type_="oauth")

    def _write_creds_file(self, work_dir, payload):
        claude_dir = work_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / ".credentials.json").write_text(json.dumps(payload))

    def test_noop_when_no_credentials_file(self, writer, work_dir):
        token = self._oauth_token()
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_noop_when_refresh_token_unchanged(self, writer, work_dir):
        self._write_creds_file(work_dir, {
            "claudeAiOauth": {"accessToken": "acc-OLD", "refreshToken": "rt-OLD"}
        })
        token = self._oauth_token(refresh="rt-OLD")
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_noop_for_anthropic_api_key_type(self, writer, work_dir):
        self._write_creds_file(work_dir, {
            "claudeAiOauth": {"accessToken": "acc-NEW", "refreshToken": "rt-NEW"}
        })
        token = _make_token({"api_key": "sk-ant-xyz"}, type_="anthropic_api_key")
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_noop_when_file_is_malformed_json(self, writer, work_dir):
        claude_dir = work_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / ".credentials.json").write_text("{ not json")
        token = self._oauth_token()
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_noop_when_claude_oauth_block_missing(self, writer, work_dir):
        self._write_creds_file(work_dir, {"somethingElse": {}})
        token = self._oauth_token()
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_noop_when_refresh_token_missing_in_file(self, writer, work_dir):
        # claudeAiOauth block present but no refreshToken (partial/garbled CLI
        # rewrite): the guard must short-circuit, never persisting refresh_token=None.
        self._write_creds_file(work_dir, {"claudeAiOauth": {"accessToken": "acc-NEW"}})
        token = self._oauth_token()
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()

    def test_persists_rotated_credentials(self, writer, work_dir):
        refreshed = {
            "claudeAiOauth": {
                "accessToken": "acc-NEW",
                "refreshToken": "rt-NEW",
                "expiresAt": 1799999999999,
                "subscriptionType": "team",
                "scopes": ["user:inference"],
            }
        }
        self._write_creds_file(work_dir, refreshed)
        token = self._oauth_token(refresh="rt-OLD", access="acc-OLD")
        mock_conn = MagicMock()
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, mock_conn)

        mock_reg.assert_called_once()
        args, _kwargs = mock_reg.call_args
        # Signature: update_refreshed_credentials(conn, token_id, new_creds, logger=...)
        assert args[0] is mock_conn
        assert args[1] == token.id   # targeted by id (==1 from _make_token)
        saved = args[2]
        # Full refreshed file is stored verbatim under raw_auth.credentials...
        assert saved["raw_auth"]["credentials"] == refreshed
        # ...and the untouched claude_json identity block is preserved.
        assert saved["raw_auth"]["claude_json"] == {"oauthAccount": {"emailAddress": "user@example.test"}}
        assert saved["refresh_token"] == "rt-NEW"
        assert saved["subscription_key"] == "acc-NEW"
        # expires_at is forced None so update_refreshed_credentials writes a NULL DB expiry — see DECISIONS.md.
        assert saved["expires_at"] is None

    def test_forces_db_expires_at_null_even_for_seconds_expiry(self, writer, work_dir):
        # A legacy/manual token whose top-level expires_at is in *seconds* (or ISO)
        # would otherwise coerce to a real DB expiry and be filtered out by
        # select_token after an idle gap. Capture must force it None regardless.
        self._write_creds_file(work_dir, {
            "claudeAiOauth": {"accessToken": "acc-NEW", "refreshToken": "rt-NEW"}
        })
        token = _make_token(
            {"subscription_key": "acc-OLD", "refresh_token": "rt-OLD",
             "expires_at": 1799999999},  # parseable seconds → would become a hard DB expiry
            type_="oauth",
        )
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_called_once()
        saved = mock_reg.call_args.args[2]
        assert saved["expires_at"] is None

    def test_persists_when_legacy_token_has_no_raw_auth(self, writer, work_dir):
        # Token registered before raw_auth capture: only top-level refresh_token.
        # A genuine rotation must still be detected against that fallback.
        self._write_creds_file(work_dir, {
            "claudeAiOauth": {"accessToken": "acc-NEW", "refreshToken": "rt-NEW"}
        })
        token = _make_token(
            {"subscription_key": "acc-OLD", "refresh_token": "rt-OLD"},
            type_="oauth",
        )
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_called_once()
        saved = mock_reg.call_args.args[2]
        assert saved["refresh_token"] == "rt-NEW"
        # Legacy path seeds a fresh raw_auth from the refreshed file (old_raw_auth was {}).
        assert saved["raw_auth"]["credentials"] == {
            "claudeAiOauth": {"accessToken": "acc-NEW", "refreshToken": "rt-NEW"}
        }

    def test_noop_when_legacy_token_refresh_unchanged(self, writer, work_dir):
        # Legacy token, CLI did NOT rotate — must not write spuriously every run.
        self._write_creds_file(work_dir, {
            "claudeAiOauth": {"accessToken": "acc-OLD", "refreshToken": "rt-OLD"}
        })
        token = _make_token(
            {"subscription_key": "acc-OLD", "refresh_token": "rt-OLD"},
            type_="oauth",
        )
        with patch("agento.modules.claude.src.config.update_refreshed_credentials") as mock_reg:
            writer.capture_refreshed_credentials(work_dir, token, MagicMock())
        mock_reg.assert_not_called()
