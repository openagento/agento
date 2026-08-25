"""Tests for ResolveAccountIdObserver."""
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira.src.observers import ResolveAccountIdObserver


@pytest.fixture(autouse=True)
def _no_real_capability():
    """`rest_capability` opens its OWN connection (the toolbox reads the row from another
    process), so a unit test that mocks only the caller's connection would dial a real DB."""
    with patch("agento.modules.jira.src.observers.rest_capability", lambda *a, **k: nullcontext("cap")):
        yield


def _make_event(name="jira"):
    event = MagicMock()
    event.name = name
    return event


def _make_config(**overrides):
    defaults = {
        "toolbox_url": "http://toolbox:3001",
        "user": "agent@test.com",
        "jira_projects": ["TEST"],
        "jira_assignee": "Agent",
        "jira_assignee_account_id": "",
    }
    defaults.update(overrides)
    return JiraConfig(**defaults)


def _make_agent_view(id=1, workspace_id=10, code="dev_01"):
    av = MagicMock()
    av.id = id
    av.workspace_id = workspace_id
    av.code = code
    av.is_active = True
    return av


_FWK = "agento.framework"
_OBS = "agento.modules.jira.src.observers"


class TestSkipConditions:
    def test_skips_non_jira_module(self):
        observer = ResolveAccountIdObserver()
        observer.execute(_make_event(name="other"))

    @patch(f"{_FWK}.bootstrap.get_module_config")
    def test_skips_when_no_toolbox_url(self, mock_get_config):
        mock_get_config.return_value = _make_config(toolbox_url="")
        observer = ResolveAccountIdObserver()
        observer.execute(_make_event())


class TestAgentViewScopeResolve:
    @patch(f"{_FWK}.workspace.get_active_agent_views")
    @patch(f"{_FWK}.db.get_connection")
    @patch(f"{_FWK}.database_config.DatabaseConfig.from_env")
    @patch(f"{_FWK}.bootstrap.get_module_config")
    def test_resolves_for_agent_view_with_credentials(
        self, mock_get_config, mock_db_config, mock_get_conn, mock_get_avs,
    ):
        mock_get_config.return_value = _make_config(
            jira_assignee_account_id="already-set"
        )
        av = _make_agent_view(id=2, code="dev_01")
        mock_get_avs.return_value = [av]
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        # Agent_view has its own jira_user but no account_id
        av_overrides = {
            "jira/jira_user": ("user@dev.com", False),
            "jira/jira_token": ("tok-123", False),
        }
        sc_values = {
            "jira/jira_user": "user@dev.com",
            "jira/jira_token": "tok-123",
        }

        with patch(f"{_FWK}.scoped_config.load_scoped_db_overrides", return_value=av_overrides), \
             patch(f"{_FWK}.config_resolver.ScopedConfigService") as mock_sc_cls, \
             patch(f"{_FWK}.scoped_config.scoped_config_set") as mock_scoped_set, \
             patch(f"{_OBS}._resolve_account_id", return_value="712020:dev-abc") as mock_resolve:
            mock_sc = MagicMock()
            mock_sc.get.side_effect = lambda path: sc_values.get(path)
            mock_sc_cls.return_value = mock_sc

            observer = ResolveAccountIdObserver()
            observer.execute(_make_event())

        assert mock_resolve.call_count == 1
        call = mock_resolve.call_args
        assert call.args == ("http://toolbox:3001",)
        assert call.kwargs["agent_view_id"] == 2
        # The observer owns the capability's whole lifetime — it runs outside any job.
        assert call.kwargs["capability_token"]
        mock_scoped_set.assert_called_once()

    @patch(f"{_FWK}.workspace.get_active_agent_views")
    @patch(f"{_FWK}.db.get_connection")
    @patch(f"{_FWK}.database_config.DatabaseConfig.from_env")
    @patch(f"{_FWK}.bootstrap.get_module_config")
    def test_skips_agent_view_without_credentials(
        self, mock_get_config, mock_db_config, mock_get_conn, mock_get_avs,
    ):
        mock_get_config.return_value = _make_config(
            jira_assignee_account_id="already-set"
        )
        av = _make_agent_view(id=3, code="no_creds")
        mock_get_avs.return_value = [av]
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        # No jira_user at agent_view scope → skip
        with patch(f"{_FWK}.scoped_config.load_scoped_db_overrides", return_value={}), \
             patch(f"{_OBS}._resolve_account_id") as mock_resolve:

            observer = ResolveAccountIdObserver()
            observer.execute(_make_event())

        mock_resolve.assert_not_called()

    @patch(f"{_FWK}.workspace.get_active_agent_views")
    @patch(f"{_FWK}.db.get_connection")
    @patch(f"{_FWK}.database_config.DatabaseConfig.from_env")
    @patch(f"{_FWK}.bootstrap.get_module_config")
    def test_skips_agent_view_with_existing_account_id(
        self, mock_get_config, mock_db_config, mock_get_conn, mock_get_avs,
    ):
        mock_get_config.return_value = _make_config(
            jira_assignee_account_id="already-set"
        )
        av = _make_agent_view(id=2, code="dev_01")
        mock_get_avs.return_value = [av]
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        # Has own user AND account_id already set → skip
        av_overrides = {
            "jira/jira_user": ("user@dev.com", False),
            "jira/jira_assignee_account_id": ("712020:exists", False),
        }

        with patch(f"{_FWK}.scoped_config.load_scoped_db_overrides", return_value=av_overrides), \
             patch(f"{_OBS}._resolve_account_id") as mock_resolve:

            observer = ResolveAccountIdObserver()
            observer.execute(_make_event())

        mock_resolve.assert_not_called()


class TestErrorHandling:
    @patch(f"{_FWK}.workspace.get_active_agent_views", side_effect=RuntimeError("DB down"))
    @patch(f"{_FWK}.db.get_connection")
    @patch(f"{_FWK}.database_config.DatabaseConfig.from_env")
    @patch(f"{_FWK}.bootstrap.get_module_config")
    def test_handles_agent_view_db_error_gracefully(
        self, mock_get_config, mock_db_config, mock_get_conn, mock_get_avs,
    ):
        mock_get_config.return_value = _make_config(
            jira_assignee_account_id="already-set"
        )
        mock_get_conn.return_value = MagicMock()

        observer = ResolveAccountIdObserver()
        observer.execute(_make_event())  # should not raise
