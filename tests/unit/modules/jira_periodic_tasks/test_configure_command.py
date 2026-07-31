"""Tests for ConfigureCommand orchestrator — project derivation, exit codes, persistence."""
from __future__ import annotations

import argparse
import logging
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.jira_periodic_tasks.src.config import PeriodicTasksConfig
from agento.modules.jira_periodic_tasks.src.configure import ConfigureReport


def _args(check=False, project=None, projects=None):
    return argparse.Namespace(check=check, project=project, projects=projects)


def _agent_view(id, code):
    av = MagicMock()
    av.id = id
    av.code = code
    return av


def _periodic(status="Periodic", field="customfield_10709", fmap=None):
    return PeriodicTasksConfig(
        jira_status=status,
        jira_frequency_field=field,
        frequency_map=fmap or {"Every 5min": "*/5 * * * *"},
    )


def _scoped_factory(instances, *, default=None, per_view=None):
    default = default if default is not None else {"core/toolbox/url": "http://toolbox:3001"}
    per_view = per_view or {}

    def make(*args, **kwargs):
        inst = MagicMock()
        vals = per_view.get(args[2], {}) if len(args) >= 3 else default
        inst.get.side_effect = lambda p: vals.get(p)
        instances.append(inst)
        return inst

    return make


_UNSET = object()


def _patch_deps(stack, *, periodic=None, admin=_UNSET, report=None, projects_default=None,
                per_view=None, avs=None, toolbox_error=None):
    instances = []
    p = {
        "load_cfg": patch("agento.framework.cli.runtime._load_framework_config",
                          return_value=(MagicMock(), None, None)),
        "get_conn": patch("agento.framework.db.get_connection", return_value=MagicMock()),
        "bootstrap": patch("agento.framework.bootstrap.bootstrap"),
        "logger": patch("agento.framework.log.get_logger",
                        return_value=logging.getLogger("test-configure-cmd")),
        "get_module_config": patch("agento.framework.bootstrap.get_module_config"),
        "scoped": patch("agento.framework.config_resolver.ScopedConfigService"),
        "load_db_overrides": patch("agento.framework.config_resolver.load_db_overrides",
                                   return_value={}),
        "config_set": patch("agento.framework.core_config.config_set"),
        "get_active_avs": patch("agento.framework.workspace.get_active_agent_views",
                                return_value=avs if avs is not None else []),
        "toolbox": patch("agento.modules.jira.src.toolbox_client.ToolboxClient"),
        "configurer": patch("agento.modules.jira_periodic_tasks.src.configure.JiraPeriodicConfigurer"),
        "render": patch("agento.modules.jira_periodic_tasks.src.configure.render_report",
                        return_value="<report>"),
        "resolve_admin": patch("agento.modules.jira_periodic_tasks.src.onboarding._resolve_admin_auth",
                               return_value={"auth_user": "a", "auth_token": "t"} if admin is _UNSET else admin),
    }
    m = {name: stack.enter_context(ctx) for name, ctx in p.items()}
    m["instances"] = instances

    default_get = {"core/toolbox/url": "http://toolbox:3001"}
    if projects_default is not None:
        default_get["jira/jira_projects"] = projects_default
    m["scoped"].side_effect = _scoped_factory(instances, default=default_get, per_view=per_view)

    m["get_module_config"].side_effect = lambda name: (
        (periodic if periodic is not None else _periodic()) if name == "jira_periodic_tasks" else {}
    )
    if toolbox_error is not None:
        m["toolbox"].return_value.jira_request.side_effect = toolbox_error
    m["configurer"].return_value.run.return_value = (
        report if report is not None else ConfigureReport(resolved_field_id="customfield_10709")
    )
    return m


def _run(args):
    from agento.modules.jira_periodic_tasks.src.commands.configure import ConfigureCommand
    with pytest.raises(SystemExit) as ei:
        ConfigureCommand().execute(args)
    return ei.value.code


# --- project derivation ---------------------------------------------------------

def test_derive_union_across_enabled_views():
    with ExitStack() as stack:
        av1, av2 = _agent_view(1, "a"), _agent_view(2, "b")
        m = _patch_deps(stack, avs=[av1, av2], per_view={
            1: {"jira/enabled": "true", "jira/jira_projects": '["AI"]'},
            2: {"jira/enabled": "true", "jira/jira_projects": '["BUG", "AI"]'},
        })
        _run(_args())
        kwargs = m["configurer"].call_args.kwargs
        assert kwargs["projects"] == ["AI", "BUG"]  # union, deduped, order-preserving


def test_skip_disabled_view():
    with ExitStack() as stack:
        av1, av2 = _agent_view(1, "a"), _agent_view(2, "b")
        m = _patch_deps(stack, avs=[av1, av2], per_view={
            1: {"jira/enabled": "false", "jira/jira_projects": '["AI"]'},
            2: {"jira/enabled": "true", "jira/jira_projects": '["BUG"]'},
        })
        _run(_args())
        assert m["configurer"].call_args.kwargs["projects"] == ["BUG"]


def test_skip_view_with_unparseable_projects():
    with ExitStack() as stack:
        av1, av2 = _agent_view(1, "a"), _agent_view(2, "b")
        m = _patch_deps(stack, avs=[av1, av2], per_view={
            1: {"jira/enabled": "true", "jira/jira_projects": "not json"},
            2: {"jira/enabled": "true", "jira/jira_projects": '["BUG"]'},
        })
        _run(_args())
        assert m["configurer"].call_args.kwargs["projects"] == ["BUG"]


def test_fallback_to_global_when_no_views():
    with ExitStack() as stack:
        m = _patch_deps(stack, avs=[], projects_default='["GLOB"]')
        _run(_args())
        assert m["configurer"].call_args.kwargs["projects"] == ["GLOB"]


def test_project_arg_overrides_derivation():
    with ExitStack() as stack:
        av1 = _agent_view(1, "a")
        m = _patch_deps(stack, avs=[av1], per_view={1: {"jira/enabled": "true", "jira/jira_projects": '["AI"]'}})
        _run(_args(project=["ONLY"]))
        assert m["configurer"].call_args.kwargs["projects"] == ["ONLY"]
        # derivation short-circuited: no per-view ScopedConfigService built (only default cfg)
        assert m["get_active_avs"].call_count == 0


def test_positional_project_used():
    with ExitStack() as stack:
        m = _patch_deps(stack)
        _run(_args(projects=["DEV"]))
        assert m["configurer"].call_args.kwargs["projects"] == ["DEV"]
        assert m["get_active_avs"].call_count == 0  # explicit → no derivation


def test_positional_and_flag_unioned():
    with ExitStack() as stack:
        m = _patch_deps(stack)
        _run(_args(project=["A"], projects=["B", "A"]))
        assert m["configurer"].call_args.kwargs["projects"] == ["A", "B"]  # union, deduped


def test_reads_config_via_per_path_never_get_module():
    with ExitStack() as stack:
        m = _patch_deps(stack, projects_default='["GLOB"]')
        _run(_args())
        # get_module_config only ever asked for the secret-free periodic module
        assert all(c.args[0] == "jira_periodic_tasks" for c in m["get_module_config"].call_args_list)
        # no ScopedConfigService instance had .get_module() invoked (no jira_token decryption)
        for inst in m["instances"]:
            inst.get_module.assert_not_called()


# --- exit codes -----------------------------------------------------------------

def test_empty_projects_exits_0():
    with ExitStack() as stack:
        _patch_deps(stack, avs=[], projects_default=None)  # no global projects either
        assert _run(_args()) == 0


def test_check_inconsistent_exits_1():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]',
                    report=ConfigureReport(resolved_field_id="f", inconsistent=True))
        assert _run(_args(check=True)) == 1


def test_check_incomplete_exits_1():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]',
                    report=ConfigureReport(resolved_field_id="f", incomplete=True))
        assert _run(_args(check=True)) == 1


def test_check_clean_exits_0():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]',
                    report=ConfigureReport(resolved_field_id="f"))
        assert _run(_args(check=True)) == 0


def test_apply_success_exits_0():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]',
                    report=ConfigureReport(resolved_field_id="f", failed=False))
        assert _run(_args()) == 0


def test_apply_failed_exits_1():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]',
                    report=ConfigureReport(resolved_field_id="f", failed=True))
        assert _run(_args()) == 1


def test_apply_without_admin_exits_1():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]', admin=None)
        assert _run(_args()) == 1  # apply requires admin


def test_toolbox_unreachable_exits_1():
    with ExitStack() as stack:
        _patch_deps(stack, projects_default='["GLOB"]', toolbox_error=Exception("refused"))
        assert _run(_args(check=True)) == 1


# --- persistence ----------------------------------------------------------------

def test_persists_field_and_status_in_apply():
    with ExitStack() as stack:
        m = _patch_deps(
            stack, projects_default='["GLOB"]',
            periodic=_periodic(status="", field=""),  # nothing stored yet → both change
            report=ConfigureReport(resolved_field_id="customfield_NEW", field_created=True),
        )
        _run(_args())
        saved = {c.args[1]: c.args[2] for c in m["config_set"].call_args_list}
        assert saved["jira_periodic_tasks/jira_frequency_field"] == "customfield_NEW"
        assert saved["jira_periodic_tasks/jira_status"] == "Periodic"


def test_check_does_not_persist():
    with ExitStack() as stack:
        m = _patch_deps(stack, projects_default='["GLOB"]',
                        periodic=_periodic(status="", field=""),
                        report=ConfigureReport(resolved_field_id="customfield_NEW"))
        _run(_args(check=True))
        m["config_set"].assert_not_called()


def test_persistence_guarded_on_failed_run():
    with ExitStack() as stack:
        m = _patch_deps(
            stack, projects_default='["GLOB"]',
            periodic=_periodic(status="Periodic", field="customfield_OLD"),
            report=ConfigureReport(resolved_field_id=None, failed=True),  # field creation failed
        )
        _run(_args())
        m["config_set"].assert_not_called()  # never corrupt config on a failed run


def test_no_change_no_persist():
    with ExitStack() as stack:
        m = _patch_deps(
            stack, projects_default='["GLOB"]',
            periodic=_periodic(status="Periodic", field="customfield_10709"),
            report=ConfigureReport(resolved_field_id="customfield_10709"),  # unchanged
        )
        _run(_args())
        m["config_set"].assert_not_called()


def test_conn_closed_in_finally():
    with ExitStack() as stack:
        m = _patch_deps(stack, projects_default='["GLOB"]')
        _run(_args())
        m["get_conn"].return_value.close.assert_called_once()
