"""Tests for SyncCommand orchestrator — per-agent_view iteration and schedule aggregation."""
from __future__ import annotations

import argparse
import logging
from unittest.mock import MagicMock, patch

from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira_periodic_tasks.src.sync import CronEntry


def _args(dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run)


def _make_agent_view(id: int, code: str):
    av = MagicMock()
    av.id = id
    av.code = code
    return av


def _make_jira_config(enabled: bool = True, **kwargs) -> JiraConfig:
    defaults = {
        "enabled": enabled,
        "toolbox_url": "http://toolbox:3001",
        "user": "ai@example.com",
        "jira_projects": ["AI"],
    }
    defaults.update(kwargs)
    return JiraConfig(**defaults)


def _make_entry(issue_key: str, agent_view_code: str = "") -> CronEntry:
    return CronEntry(
        issue_key=issue_key,
        summary="t",
        frequency_label="Co 5min",
        cron_expression="*/5 * * * *",
        agent_view_code=agent_view_code,
    )


def _patch_orchestrator_deps(stack: list):
    """Apply common patches; returns a dict of all mocks for assertions."""
    patches = {
        "load_cfg": patch("agento.framework.cli.runtime._load_framework_config",
                          return_value=(MagicMock(), None, None)),
        "get_conn": patch("agento.framework.db.get_connection", return_value=MagicMock()),
        "bootstrap": patch("agento.framework.bootstrap.bootstrap"),
        "logger": patch(
            "agento.modules.jira_periodic_tasks.src.commands.sync.get_logger"
            if False else "agento.framework.log.get_logger",
            return_value=logging.getLogger("test-orchestrator"),
        ),
        "filelock": patch("agento.framework.lock.FileLock"),
        "toolbox": patch(
            "agento.modules.jira_periodic_tasks.src.commands.sync.ToolboxClient"
        ),
        "syncer_cls": patch(
            "agento.modules.jira_periodic_tasks.src.commands.sync.JiraCronSync"
        ),
        "get_module_config": patch(
            "agento.framework.bootstrap.get_module_config",
            return_value=MagicMock(),
        ),
        "get_scoped_config": patch("agento.framework.config_resolver.ScopedConfigService"),
        "get_active_avs": patch("agento.framework.workspace.get_active_agent_views"),
    }
    mocks = {name: stack.enter_context(p) for name, p in patches.items()}
    # FileLock as a no-op context manager
    mocks["filelock"].return_value.__enter__ = lambda s: s
    mocks["filelock"].return_value.__exit__ = MagicMock(return_value=False)
    return mocks


def test_execute_no_agent_views_falls_back_to_global_config():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        m["get_active_avs"].return_value = []
        global_cfg = _make_jira_config()
        m["get_module_config"].side_effect = lambda name, *a, **kw: (
            global_cfg if name == "jira" else MagicMock()
        )

        syncer = MagicMock()
        syncer.sync_view.return_value = [_make_entry("AI-1")]
        m["syncer_cls"].return_value = syncer

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        m["syncer_cls"].assert_called_once()
        # Global path: no agent_view_id/code passed
        _, kwargs = m["syncer_cls"].call_args
        assert kwargs.get("agent_view_id") is None
        assert kwargs.get("agent_view_code", "") == ""


def test_execute_iterates_active_agent_views_per_view():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        av1 = _make_agent_view(1, "mieszko")
        av2 = _make_agent_view(2, "zyga")
        m["get_active_avs"].return_value = [av1, av2]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()

        syncer = MagicMock()
        syncer.sync_view.side_effect = [
            [_make_entry("AI-3", "mieszko")],
            [_make_entry("AI-87", "zyga")],
        ]
        m["syncer_cls"].return_value = syncer

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        assert m["syncer_cls"].call_count == 2
        first_kwargs = m["syncer_cls"].call_args_list[0].kwargs
        second_kwargs = m["syncer_cls"].call_args_list[1].kwargs
        assert first_kwargs["agent_view_id"] == 1
        assert first_kwargs["agent_view_code"] == "mieszko"
        assert second_kwargs["agent_view_id"] == 2
        assert second_kwargs["agent_view_code"] == "zyga"


def test_execute_skips_disabled_view():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        av1 = _make_agent_view(1, "mieszko")
        av2 = _make_agent_view(2, "zyga")
        m["get_active_avs"].return_value = [av1, av2]
        m["get_scoped_config"].return_value.get_module.side_effect = [
            _make_jira_config(enabled=False),
            _make_jira_config(enabled=True),
        ]
        syncer = MagicMock()
        syncer.sync_view.return_value = []
        m["syncer_cls"].return_value = syncer

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        # Only zyga's syncer constructed; mieszko's view was disabled
        assert m["syncer_cls"].call_count == 1
        assert m["syncer_cls"].call_args.kwargs["agent_view_code"] == "zyga"


def test_execute_skips_view_with_no_resolvable_config():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        av1 = _make_agent_view(1, "mieszko")
        av2 = _make_agent_view(2, "zyga")
        m["get_active_avs"].return_value = [av1, av2]
        m["get_scoped_config"].return_value.get_module.side_effect = [None, _make_jira_config()]
        syncer = MagicMock()
        syncer.sync_view.return_value = []
        m["syncer_cls"].return_value = syncer

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        assert m["syncer_cls"].call_count == 1
        assert m["syncer_cls"].call_args.kwargs["agent_view_code"] == "zyga"


def test_execute_continues_when_first_view_raises():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        av1 = _make_agent_view(1, "mieszko")
        av2 = _make_agent_view(2, "zyga")
        m["get_active_avs"].return_value = [av1, av2]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()

        first_syncer = MagicMock()
        first_syncer.sync_view.side_effect = RuntimeError("toolbox down for view 1")
        second_syncer = MagicMock()
        second_syncer.sync_view.return_value = [_make_entry("AI-87", "zyga")]
        m["syncer_cls"].side_effect = [first_syncer, second_syncer]

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())  # must not raise

        first_syncer.sync_view.assert_called_once()
        second_syncer.sync_view.assert_called_once()


def test_execute_aggregates_entries_from_every_view():
    """Every view's entries reach the ``schedule`` table; the root renderer reads it."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        av1 = _make_agent_view(1, "mieszko")
        av2 = _make_agent_view(2, "zyga")
        m["get_active_avs"].return_value = [av1, av2]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()

        syncer = MagicMock()
        syncer.sync_view.side_effect = [
            [_make_entry("AI-3", "mieszko")], [_make_entry("AI-87", "zyga")],
        ]
        m["syncer_cls"].return_value = syncer

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        assert syncer.sync_view.call_count == 2


def test_execute_never_invokes_the_crontab_binary():
    """The crontab belongs to root now — a sync must not touch it."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        m["get_active_avs"].return_value = [_make_agent_view(1, "mieszko")]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()
        syncer = MagicMock()
        syncer.sync_view.return_value = []
        m["syncer_cls"].return_value = syncer
        run = stack.enter_context(patch("subprocess.run"))

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        assert not any("crontab" in str(c) for c in run.call_args_list)


def test_execute_passes_dry_run_through():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        m["get_active_avs"].return_value = [_make_agent_view(1, "mieszko")]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()
        syncer = MagicMock()
        syncer.sync_view.return_value = []
        m["syncer_cls"].return_value = syncer

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args(dry_run=True))

        syncer.sync_view.assert_called_once_with(dry_run=True)
