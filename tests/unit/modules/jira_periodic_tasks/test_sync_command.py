"""Tests for SyncCommand orchestrator — per-agent_view iteration and crontab aggregation."""
from __future__ import annotations

import argparse
import logging
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira_periodic_tasks.src.crontab import CronEntry


@pytest.fixture(autouse=True)
def _no_real_capability():
    """`rest_capability` opens its OWN connection (the toolbox reads the row from another
    process), so a unit test that mocks only the caller's connection would dial a real DB."""
    with patch("agento.modules.jira_periodic_tasks.src.commands.sync.rest_capability", lambda *a, **k: nullcontext("cap")):
        yield

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
        "crontab_mgr": patch(
            "agento.modules.jira_periodic_tasks.src.commands.sync.CrontabManager"
        ),
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


def test_execute_without_any_agent_view_exits_and_never_syncs():
    """No global fallback: a viewless sync has no scope to mint a toolbox
    capability for, so every REST call would be refused."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        m["get_active_avs"].return_value = []

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        with pytest.raises(SystemExit) as ei:
            SyncCommand().execute(_args())

        assert ei.value.code == 1
        m["syncer_cls"].assert_not_called()


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


def test_execute_writes_single_crontab_with_combined_entries():
    """The crontab is one shared file. Per-view sync must NOT write its own
    crontab — the orchestrator aggregates entries and writes once. Otherwise
    each subsequent view's write erases the previous view's entries."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        av1 = _make_agent_view(1, "mieszko")
        av2 = _make_agent_view(2, "zyga")
        m["get_active_avs"].return_value = [av1, av2]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()

        first_entries = [_make_entry("AI-3", "mieszko")]
        second_entries = [_make_entry("AI-87", "zyga")]
        syncer = MagicMock()
        syncer.sync_view.side_effect = [first_entries, second_entries]
        m["syncer_cls"].return_value = syncer

        crontab_mgr = m["crontab_mgr"].return_value
        crontab_mgr.apply_managed.return_value = True

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args())

        # Crontab applied exactly once, regardless of how many views,
        # with combined entries from both views.
        assert crontab_mgr.apply_managed.call_count == 1
        combined_entries = crontab_mgr.apply_managed.call_args[0][0]
        keys = [e.issue_key for e in combined_entries]
        assert "AI-3" in keys and "AI-87" in keys


def test_execute_passes_dry_run_through():
    from contextlib import ExitStack

    with ExitStack() as stack:
        m = _patch_orchestrator_deps(stack)
        m["get_active_avs"].return_value = [_make_agent_view(1, "mieszko")]
        m["get_scoped_config"].return_value.get_module.return_value = _make_jira_config()
        syncer = MagicMock()
        syncer.sync_view.return_value = []
        m["syncer_cls"].return_value = syncer

        crontab_mgr = m["crontab_mgr"].return_value
        crontab_mgr.apply_managed.return_value = False

        from agento.modules.jira_periodic_tasks.src.commands.sync import SyncCommand
        SyncCommand().execute(_args(dry_run=True))

        syncer.sync_view.assert_called_once_with(dry_run=True)
        assert crontab_mgr.apply_managed.call_args.kwargs["dry_run"] is True
