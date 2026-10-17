"""Tests for ScopedConfigService — the single ENV -> DB -> config.json resolver.

Scoping is pre-merged (build_scoped_overrides); these tests patch the override
builders (tested separately) and focus on the service's resolution logic.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.config_resolver import (
    ScopedConfigService,
    env_key_to_path,
    path_to_env_key,
)


@pytest.fixture
def patched_overrides():
    """Patch the DB-override builders the service uses (lazy-imported from scoped_config)."""
    with patch("agento.framework.scoped_config.build_scoped_overrides") as merged, \
         patch("agento.framework.scoped_config.load_scoped_db_overrides") as scope_only:
        merged.return_value = {}
        scope_only.return_value = {}
        yield merged, scope_only


class TestGet:
    def test_env_wins_over_db(self, monkeypatch, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"jira/token": ("db-token", False)}
        monkeypatch.setenv("CONFIG__JIRA__TOKEN", "env-token")

        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        assert svc.get("jira/token") == "env-token"

    def test_db_value_when_no_env(self, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"agent_view/model": ("opus-4.6", False)}

        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        assert svc.get("agent_view/model") == "opus-4.6"

    def test_tool_path_env_key_format(self, monkeypatch, patched_overrides):
        monkeypatch.setenv("CONFIG__MYMOD__TOOLS__SEARCH__URL", "env-url")
        svc = ScopedConfigService(MagicMock())
        assert svc.get("mymod/tools/search/url") == "env-url"

    def test_returns_string_not_coerced(self, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"agent_view/scheduling/priority": ("80", False)}
        svc = ScopedConfigService(MagicMock())
        # priority is an integer-typed field, but .get stays string (callers coerce)
        assert svc.get("agent_view/scheduling/priority") == "80"

    @patch("agento.framework.config_resolver.read_config_defaults", return_value={"token": "cfg-val"})
    @patch("agento.framework.bootstrap.get_manifests")
    def test_config_json_fallback(self, mock_manifests, _defaults, patched_overrides):
        m = MagicMock()
        m.name = "jira"
        m.path = "/some/path"
        mock_manifests.return_value = [m]
        svc = ScopedConfigService(MagicMock())
        assert svc.get("jira/token") == "cfg-val"

    @patch("agento.framework.config_resolver.read_config_defaults", return_value={})
    @patch("agento.framework.bootstrap.get_manifests", return_value=[])
    def test_returns_none_when_nothing(self, _m, _d, patched_overrides):
        svc = ScopedConfigService(MagicMock())
        assert svc.get("unknown/field") is None

    @patch("agento.framework.bootstrap.get_manifests",
           return_value=[SimpleNamespace(name="mymod", config={"token": {"type": "obscure"}})])
    def test_encrypted_db_value_decrypted(self, _m, monkeypatch, patched_overrides):
        monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "test-secret-key")
        from agento.framework.crypto import encrypt
        merged, _ = patched_overrides
        merged.return_value = {"mymod/token": (encrypt("secret-token"), True)}

        svc = ScopedConfigService(MagicMock())
        assert svc.get("mymod/token") == "secret-token"


class TestGetModule:
    @patch("agento.framework.config_resolver.read_config_defaults")
    @patch("agento.framework.bootstrap.get_manifests")
    def test_resolves_and_coerces(self, mock_manifests, mock_defaults, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"core/max_workers": ("3", False)}
        m = MagicMock()
        m.name = "core"
        m.path = "/p"
        m.config = {"max_workers": {"type": "integer"}, "label": {"type": "string"}}
        m.provides = {}
        mock_manifests.return_value = [m]
        mock_defaults.return_value = {"label": "Core"}

        svc = ScopedConfigService(MagicMock())
        cfg = svc.get_module("core")
        assert cfg["max_workers"] == 3  # coerced to int
        assert cfg["label"] == "Core"  # config.json fallback

    @patch("agento.framework.bootstrap.get_manifests", return_value=[])
    def test_unknown_module_returns_none(self, _m, patched_overrides):
        svc = ScopedConfigService(MagicMock())
        assert svc.get_module("nope") is None

    @patch("agento.framework.module_loader.import_class")
    @patch("agento.framework.config_resolver.read_config_defaults", return_value={"enabled": True})
    @patch("agento.framework.bootstrap.get_manifests")
    def test_converts_to_typed_config_class(self, mock_manifests, _defaults, mock_import, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"testmod/url": ("https://test.com", False)}
        m = MagicMock()
        m.name = "testmod"
        m.path = "/p"
        m.config = {"url": {"type": "string"}, "enabled": {"type": "boolean"}}
        m.provides = {"config_class": "src.config.TestConfig"}
        mock_manifests.return_value = [m]

        class FakeConfig:
            @classmethod
            def from_dict(cls, data):
                return {"typed": True, **data}

        mock_import.return_value = FakeConfig

        cfg = ScopedConfigService(MagicMock()).get_module("testmod")
        assert cfg["typed"] is True
        assert cfg["url"] == "https://test.com"
        assert cfg["enabled"] is True


class TestResolveFieldWithSource:
    def test_source_env(self, monkeypatch, patched_overrides):
        monkeypatch.setenv("CONFIG__JIRA__HOST", "env-host")
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        rv, inherited = svc.resolve_field_with_source("jira", "host", {"type": "string"}, {})
        assert rv.value == "env-host"
        assert rv.source == "env"
        assert inherited is False

    def test_source_db_set_at_scope(self, patched_overrides):
        merged, scope_only = patched_overrides
        merged.return_value = {"jira/host": ("av-host", False)}
        scope_only.return_value = {"jira/host": ("av-host", False)}
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        rv, inherited = svc.resolve_field_with_source("jira", "host", {"type": "string"}, {})
        assert rv.source == "db"
        assert inherited is False

    def test_source_db_inherited(self, patched_overrides):
        merged, scope_only = patched_overrides
        # value present in merged (from a parent scope) but NOT at the requested scope
        merged.return_value = {"jira/host": ("global-host", False)}
        scope_only.return_value = {}
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        rv, inherited = svc.resolve_field_with_source("jira", "host", {"type": "string"}, {})
        assert rv.source == "db"
        assert inherited is True

    def test_source_json(self, patched_overrides):
        svc = ScopedConfigService(MagicMock())
        rv, inherited = svc.resolve_field_with_source("jira", "host", {"type": "string"}, {"host": "cfg"})
        assert rv.source == "config.json"
        assert inherited is False


class TestWorkspaceId:
    def test_known_workspace_id_skips_lookup_query(self, patched_overrides):
        merged, _ = patched_overrides
        conn = MagicMock()
        ScopedConfigService(conn, scope="agent_view", scope_id=7, workspace_id=3)
        # workspace_id was supplied -> no agent_view->workspace lookup query
        conn.cursor.assert_not_called()
        # and it was passed through to the override builder
        assert merged.call_args.kwargs["workspace_id"] == 3
        assert merged.call_args.kwargs["agent_view_id"] == 7

    def test_missing_workspace_id_triggers_lookup(self, patched_overrides):
        merged, _ = patched_overrides
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {"workspace_id": 9}
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        ScopedConfigService(conn, scope="agent_view", scope_id=7)
        assert merged.call_args.kwargs["workspace_id"] == 9


class TestOverrides:
    def test_exposes_merged_dict(self, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"agent_view/provider": ("codex", False)}
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        assert svc.overrides == {"agent_view/provider": ("codex", False)}


class TestEnvKeyToPath:
    @pytest.mark.parametrize("path", [
        "agent_view/provider",
        "agent_view/model",
        "agent_view/codex/approval_mode",
        "agent_view/scheduling/priority",
        "mymod/tools/search/url",
        "jira/host",
    ])
    def test_round_trips_with_path_to_env_key(self, path):
        assert env_key_to_path(path_to_env_key(path)) == path

    def test_maps_provider_specific_env_key(self):
        assert env_key_to_path("CONFIG__AGENT_VIEW__CODEX__APPROVAL_MODE") == (
            "agent_view/codex/approval_mode"
        )


class TestResolveAll:
    @patch("agento.framework.bootstrap.get_manifests", return_value=[])
    def test_includes_db_override_keys(self, _m, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {
            "agent_view/model": ("opus", False),
            "jira/host": ("https://db", False),
        }
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        resolved = svc.resolve_all()
        assert resolved["agent_view/model"] == "opus"
        assert resolved["jira/host"] == "https://db"

    @patch("agento.framework.bootstrap.get_manifests", return_value=[])
    def test_env_beats_db(self, _m, monkeypatch, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {"agent_view/model": ("db-model", False)}
        monkeypatch.setenv("CONFIG__AGENT_VIEW__MODEL", "env-model")
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        assert svc.resolve_all()["agent_view/model"] == "env-model"

    @patch("agento.framework.bootstrap.get_manifests", return_value=[])
    def test_env_only_key_with_no_db_row_appears(self, _m, monkeypatch, patched_overrides):
        merged, _ = patched_overrides
        merged.return_value = {}
        monkeypatch.setenv("CONFIG__AGENT_VIEW__CODEX__APPROVAL_MODE", "on-failure")
        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        assert svc.resolve_all()["agent_view/codex/approval_mode"] == "on-failure"

    @patch("agento.framework.config_resolver.read_config_defaults",
           return_value={"provider": "claude"})
    @patch("agento.framework.bootstrap.get_manifests")
    def test_config_json_only_default_appears(self, mock_manifests, _defaults, patched_overrides):
        # A declared module field with a config.json default and NO DB/ENV row
        # must still be surfaced (full effective config).
        merged, _ = patched_overrides
        merged.return_value = {}
        m = MagicMock()
        m.name = "agent_view"
        m.path = "/p"
        m.config = {"provider": {"type": "string"}}
        mock_manifests.return_value = [m]

        svc = ScopedConfigService(MagicMock(), scope="agent_view", scope_id=1)
        assert svc.resolve_all()["agent_view/provider"] == "claude"
