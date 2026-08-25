"""Tests for PublishCommand — per-agent-view publishing and global fallback."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.database_config import DatabaseConfig
from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira.src.models import JiraIssue, TaskAction, TaskPriority, TaskSource


@pytest.fixture(autouse=True)
def _no_real_capability():
    """`rest_capability` opens its OWN connection (the toolbox reads the row from another
    process), so a unit test that mocks only the caller's connection would dial a real DB."""
    with patch("agento.modules.jira.src.commands.publish.rest_capability", lambda *a, **k: nullcontext("cap")):
        yield

def _make_args(kind: str, issue_key: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(kind=kind, issue_key=issue_key)


def _make_task(
    issue_key: str,
    updated: str | None,
    *,
    reporter: str | None = None,
    reporter_account_id: str | None = None,
    reporter_email: str | None = None,
) -> TaskAction:
    issue = JiraIssue(
        key=issue_key,
        summary="Test task",
        status="To Do",
        updated=updated,
        reporter=reporter,
        reporter_account_id=reporter_account_id,
        reporter_email=reporter_email,
    )
    return TaskAction(
        source=TaskSource.TODO_ASSIGNED,
        priority=TaskPriority.HIGH,
        issue=issue,
        reason="assigned",
    )


def _make_jira_config(**kwargs):
    defaults = {
        "enabled": True,
        "toolbox_url": "http://toolbox:3001",
        "user": "ai@example.com",
        "jira_assignee": "ai@example.com",
    }
    defaults.update(kwargs)
    return JiraConfig(**defaults)


def _make_agent_view(id=1, workspace_id=1, code="dev"):
    av = MagicMock()
    av.id = id
    av.workspace_id = workspace_id
    av.code = code
    return av


class TestCmdPublishTodoDispatch:
    """PublishCommand jira-todo (no issue_key) — per-agent-view publishing."""

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_passes_updated_to_publish_todo(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av = _make_agent_view()
        mock_get_avs.return_value = [av]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        task = _make_task("AI-6", updated="2026-02-24T16:45:00.000+0000")
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.return_value = [task]
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        mock_publisher.publish_todo.assert_called_once()
        _, kwargs = mock_publisher.publish_todo.call_args
        assert kwargs.get("updated") == "2026-02-24T16:45:00.000+0000"
        assert kwargs.get("agent_view_id") == 1

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_passes_issue_key_to_publish_todo(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av = _make_agent_view()
        mock_get_avs.return_value = [av]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        task = _make_task("AI-6", updated="2026-02-24T16:45:00.000+0000")
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.return_value = [task]
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        _, kwargs = mock_publisher.publish_todo.call_args
        assert kwargs.get("reference_id") == "AI-6"

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_no_tasks_skips_publish(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av = _make_agent_view()
        mock_get_avs.return_value = [av]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.return_value = []
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        mock_publisher.publish_todo.assert_not_called()

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_skips_disabled_agent_view(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        mock_get_avs.return_value = [_make_agent_view()]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config(enabled=False)

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        mock_publisher.publish_todo.assert_not_called()

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_first_agent_view_error_continues_to_next(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        from agento.modules.jira.src.toolbox_client import ToolboxAPIError

        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av1 = _make_agent_view(id=1, code="mieszko")
        av2 = _make_agent_view(id=2, code="zyga")
        mock_get_avs.return_value = [av1, av2]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        task = _make_task("AI-42", updated="2026-04-24T10:00:00.000+0000")
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.side_effect = [
            ToolboxAPIError(500, "Jira API not configured"),
            [task],
        ]
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        mock_publisher.publish_todo.assert_called_once()
        _, kwargs = mock_publisher.publish_todo.call_args
        assert kwargs.get("agent_view_id") == 2
        assert kwargs.get("reference_id") == "AI-42"

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority")
    def test_config_resolution_error_continues_to_next_agent_view(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        """If get_scoped_config OR resolve_publish_priority raises for one
        agent_view, subsequent agent_views must still publish."""
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av1 = _make_agent_view(id=1, code="mieszko")
        av2 = _make_agent_view(id=2, code="zyga")
        mock_get_avs.return_value = [av1, av2]

        good_cfg = _make_jira_config()
        mock_scoped_config.return_value.get_module.side_effect = [RuntimeError("bad config row"), good_cfg]
        mock_priority.return_value = 50

        task = _make_task("AI-99", updated="2026-05-14T10:00:00.000+0000")
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.return_value = [task]
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        mock_publisher.publish_todo.assert_called_once()
        _, kwargs = mock_publisher.publish_todo.call_args
        assert kwargs.get("agent_view_id") == 2

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_publishes_all_tasks_for_agent_view(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        """Publisher must enqueue every task returned by get_todo_tasks, not just tasks[0]."""
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av = _make_agent_view()
        mock_get_avs.return_value = [av]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        tasks = [
            _make_task("AI-72", updated="2026-05-26T07:13:00.000+0000"),
            _make_task("AI-100", updated="2026-05-26T07:13:00.000+0000"),
            _make_task("AI-107", updated="2026-05-26T07:13:00.000+0000"),
        ]
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.return_value = tasks
        mock_builder_cls.return_value = builder
        mock_publisher.publish_todo.return_value = True

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        assert mock_publisher.publish_todo.call_count == 3
        published_keys = [call.kwargs["reference_id"] for call in mock_publisher.publish_todo.call_args_list]
        assert published_keys == ["AI-72", "AI-100", "AI-107"]

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_deduped_skips_do_not_inflate_published_count(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        """When publish_todo returns False (active/duplicate), the X-of-Y log must reflect actual inserts."""
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        av = _make_agent_view()
        mock_get_avs.return_value = [av]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        tasks = [
            _make_task("AI-72", updated="2026-05-26T07:13:00.000+0000"),
            _make_task("AI-100", updated="2026-05-26T07:13:00.000+0000"),
            _make_task("AI-107", updated="2026-05-26T07:13:00.000+0000"),
        ]
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)
        builder.get_todo_tasks.return_value = tasks
        mock_builder_cls.return_value = builder
        mock_publisher.publish_todo.side_effect = [True, False, True]

        logger_instance = MagicMock()
        mock_logger.return_value = logger_instance

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        assert mock_publisher.publish_todo.call_count == 3
        summary_calls = [
            call for call in logger_instance.info.call_args_list
            if "Published %d of %d todo jobs for agent_view %d" in call.args[0]
        ]
        assert len(summary_calls) == 1
        assert summary_calls[0].args[1:] == (2, 3, 1)

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_forwards_reporter_fallback_requester(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        from agento.framework.job_models import RequesterTrust

        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        mock_get_avs.return_value = [_make_agent_view()]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        task = _make_task(
            "AI-6", updated="2026-02-24T16:45:00.000+0000",
            reporter="Reporter", reporter_account_id="rep-1", reporter_email="rep@example.com",
        )
        builder = MagicMock()
        builder.get_status_change.return_value = (None, True)  # no transition -> reporter fallback
        builder.get_todo_tasks.return_value = [task]
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        requester = mock_publisher.publish_todo.call_args.kwargs["requester"]
        assert requester is not None
        assert requester.key == "jira:rep-1"
        assert requester.trust is RequesterTrust.ACCOUNT
        assert requester.meta["basis"] == "reporter"
        assert requester.meta["fallback_reason"] == "no_status_transition"

    @patch("agento.modules.jira.src.commands.publish._publisher")
    @patch("agento.modules.jira.src.commands.publish.TaskListBuilder")
    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    @patch("agento.framework.config_resolver.ScopedConfigService")
    @patch("agento.framework.agent_view_runtime.resolve_publish_priority", return_value=50)
    def test_forwards_status_change_actor_requester(
        self, mock_priority, mock_scoped_config, mock_get_avs,
        mock_bootstrap, mock_logger, mock_toolbox_cls, mock_builder_cls, mock_publisher,
    ):
        from agento.framework.job_models import RequesterTrust

        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        mock_get_avs.return_value = [_make_agent_view()]
        mock_scoped_config.return_value.get_module.return_value = _make_jira_config()

        task = _make_task("AI-6", updated="2026-02-24T16:45:00.000+0000")
        change = {
            "id": "77", "created": "2026-02-24T16:00:00.000+0000",
            "author": {"accountId": "mover-9", "emailAddress": "mover@example.com", "displayName": "Mover"},
        }
        builder = MagicMock()
        builder.get_status_change.return_value = (change, True)
        builder.get_todo_tasks.return_value = [task]
        mock_builder_cls.return_value = builder

        from agento.modules.jira.src.commands.publish import PublishCommand
        PublishCommand().execute(_make_args("jira-todo"))

        requester = mock_publisher.publish_todo.call_args.kwargs["requester"]
        assert requester is not None
        assert requester.key == "jira:mover-9"
        assert requester.trust is RequesterTrust.ACCOUNT
        assert requester.meta["basis"] == "status_change"
        assert requester.meta["changelog_id"] == "77"


class TestCmdPublishWithoutAnyAgentView:
    """The global-config fallback is gone: a viewless publish has no scope to mint a
    toolbox capability for, so it must fail loudly instead of running unauthenticated."""

    @patch("agento.modules.jira.src.commands.publish.ToolboxClient")
    @patch("agento.modules.jira.src.commands.publish.get_logger")
    @patch("agento.modules.jira.src.commands.publish._get_connection_and_bootstrap")
    @patch("agento.framework.workspace.get_active_agent_views")
    def test_publish_without_any_agent_view_exits_and_never_builds_a_client(
        self, mock_get_avs, mock_bootstrap, mock_logger, mock_toolbox_cls,
    ):
        conn = MagicMock()
        mock_bootstrap.return_value = (DatabaseConfig(), conn)
        mock_get_avs.return_value = []

        from agento.modules.jira.src.commands.publish import PublishCommand

        with pytest.raises(SystemExit) as exc:
            PublishCommand().execute(_make_args("jira-todo"))
        assert exc.value.code == 1
        mock_toolbox_cls.assert_not_called()
