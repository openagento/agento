"""Tests for JiraConfig, DatabaseConfig, and ConsumerConfig."""
from __future__ import annotations

import pytest

from agento.framework.consumer_config import ConsumerConfig
from agento.framework.database_config import DatabaseConfig
from agento.modules.jira.src.config import JiraConfig


class TestJiraConfig:
    def test_from_dict_basic(self):
        data = {
            "toolbox_url": "http://localhost:3001",
            "user": "bot@test.com",
            "jira_projects": ["TEST"],
            "jira_assignee": "user@test.com",
        }
        config = JiraConfig.from_dict(data)

        assert config.toolbox_url == "http://localhost:3001"
        assert config.user == "bot@test.com"
        assert config.jira_projects == ["TEST"]
        assert config.jira_assignee == "user@test.com"

    def test_from_dict_multiple_projects(self):
        data = {
            "jira_projects": ["AI", "K3"],
        }
        config = JiraConfig.from_dict(data)

        assert config.jira_projects == ["AI", "K3"]
        assert config.jira_project_jql == "project IN (AI, K3)"

    def test_from_dict_comma_separated_projects(self):
        data = {
            "jira_projects": "AI, K3",
        }
        config = JiraConfig.from_dict(data)

        assert config.jira_projects == ["AI", "K3"]

    def test_single_project_jql(self):
        config = JiraConfig(jira_projects=["AI"])
        assert config.jira_project_jql == "project = AI"

    def test_config_immutable(self, sample_config):
        with pytest.raises(AttributeError):
            sample_config.jira_projects = ["OTHER"]

    def test_defaults(self):
        data = {}
        config = JiraConfig.from_dict(data)

        assert config.enabled is True
        assert config.toolbox_url == ""
        assert config.user == ""
        assert config.jira_assignee == ""

    def test_enabled_true_by_default(self):
        config = JiraConfig.from_dict({})
        assert config.enabled is True

    def test_enabled_false_from_bool(self):
        config = JiraConfig.from_dict({"enabled": False})
        assert config.enabled is False

    def test_enabled_false_from_string_zero(self):
        config = JiraConfig.from_dict({"enabled": "0"})
        assert config.enabled is False

    def test_enabled_false_from_int_zero(self):
        config = JiraConfig.from_dict({"enabled": 0})
        assert config.enabled is False

    def test_enabled_true_from_string_one(self):
        config = JiraConfig.from_dict({"enabled": "1"})
        assert config.enabled is True


class TestDatabaseConfig:
    def test_from_env_defaults(self):
        config = DatabaseConfig.from_env()

        assert config.mysql_host == "mysql"
        assert config.mysql_port == 3306
        assert config.mysql_database == "cron_agent"

    def test_from_env_with_env(self, monkeypatch):
        monkeypatch.setenv("MYSQL_HOST", "dbhost")
        monkeypatch.setenv("MYSQL_PORT", "3307")
        monkeypatch.setenv("MYSQL_DATABASE", "mydb")

        config = DatabaseConfig.from_env()

        assert config.mysql_host == "dbhost"
        assert config.mysql_port == 3307
        assert config.mysql_database == "mydb"


class TestConsumerConfig:
    def test_from_env_with_values(self, monkeypatch):
        monkeypatch.setenv("AGENTO_CONSUMER_MAX_WORKERS", "4")
        monkeypatch.setenv("AGENTO_CONSUMER_POLL_INTERVAL", "10.0")

        config = ConsumerConfig.from_env()

        assert config.concurrency == 4
        assert config.poll_interval == 10.0

    def test_from_env_defaults(self):
        config = ConsumerConfig.from_env()

        assert config.concurrency == 10
        assert config.poll_interval == 5.0
