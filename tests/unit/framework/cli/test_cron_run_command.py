"""Tests for `agento cron:run <module> <command…>` — the crontab's only entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.cli.cron import CronRunCommand


def _args(module: str, *inner: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    CronRunCommand().configure(parser)
    return parser.parse_args([module, *inner])


def _dirs(*names: str) -> list[Path]:
    return [Path("/modules") / n for n in names]


class _Recorder:
    """Stand-in for a module command — records that it ran."""

    def __init__(self, exit_code: int | None = None):
        self.ran = False
        self.parsed: argparse.Namespace | None = None
        self.exit_code = exit_code

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--flag", default="")

    def execute(self, args: argparse.Namespace) -> None:
        self.ran = True
        self.parsed = args
        if self.exit_code is not None:
            sys.exit(self.exit_code)


def _patch(installed, enabled, commands):
    return (
        patch("agento.framework.module_discovery.resolve_module_root", return_value=None),
        patch("agento.framework.module_discovery.iter_module_dirs", return_value=_dirs(*installed)),
        patch("agento.framework.module_discovery.iter_enabled_module_dirs", return_value=_dirs(*enabled)),
        patch("agento.framework.commands.get_commands", return_value=commands),
    )


def test_a_disabled_module_exits_zero_silently(capsys):
    """A module disabled after the crontab was rendered must not fail the cron job."""
    patches = _patch(["jira_periodic_tasks"], [], {})
    with patches[0], patches[1], patches[2], pytest.raises(SystemExit) as exc:
        CronRunCommand().execute(_args("jira_periodic_tasks", "jira:periodic:sync"))
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_a_disabled_module_is_never_looked_up_in_the_registry():
    """Dispatch must short-circuit before the command registry is consulted."""
    patches = _patch(["jira_periodic_tasks"], [], {})
    with patches[0], patches[1], patches[2], \
         patch("agento.framework.commands.get_commands") as get_commands, pytest.raises(SystemExit):
        CronRunCommand().execute(_args("jira_periodic_tasks", "jira:periodic:sync"))
    get_commands.assert_not_called()


def test_an_enabled_modules_command_runs_in_process():
    recorder = _Recorder()
    patches = _patch(["jira_periodic_tasks"], ["jira_periodic_tasks"], {"jira:periodic:sync": recorder})
    with patches[0], patches[1], patches[2], patches[3]:
        CronRunCommand().execute(_args("jira_periodic_tasks", "jira:periodic:sync", "--flag", "x"))
    assert recorder.ran
    assert recorder.parsed.flag == "x"


def test_the_inner_commands_exit_status_propagates():
    recorder = _Recorder(exit_code=3)
    patches = _patch(["jira_periodic_tasks"], ["jira_periodic_tasks"], {"jira:periodic:sync": recorder})
    with patches[0], patches[1], patches[2], patches[3], pytest.raises(SystemExit) as exc:
        CronRunCommand().execute(_args("jira_periodic_tasks", "jira:periodic:sync"))
    assert exc.value.code == 3


def test_an_unknown_module_exits_non_zero_with_one_line(capsys):
    patches = _patch([], [], {})
    with patches[0], patches[1], patches[2], pytest.raises(SystemExit) as exc:
        CronRunCommand().execute(_args("nope", "jira:periodic:sync"))
    assert exc.value.code == 1
    assert capsys.readouterr().err.strip().splitlines() == [
        "cron:run: unknown module 'nope'",
    ]


def test_an_unknown_command_exits_non_zero(capsys):
    patches = _patch(["jira_periodic_tasks"], ["jira_periodic_tasks"], {})
    with patches[0], patches[1], patches[2], patches[3], pytest.raises(SystemExit) as exc:
        CronRunCommand().execute(_args("jira_periodic_tasks", "jira:nope"))
    assert exc.value.code == 1
    assert "unknown command" in capsys.readouterr().err


def test_the_fd_delivered_store_is_resolvable_inside_the_dispatched_command():
    """In-process dispatch keeps the store; an ``exec`` would discard it."""
    from agento.framework import store_env

    store_env.reset()
    store_env._store["MYSQL_HOST"] = "db.internal"
    seen = {}

    class _StoreReader(_Recorder):
        def execute(self, args):
            seen["host"] = store_env.get("MYSQL_HOST")

    reader = _StoreReader()
    patches = _patch(["m"], ["m"], {"x:y": reader})
    try:
        with patches[0], patches[1], patches[2], patches[3]:
            CronRunCommand().execute(_args("m", "x:y"))
    finally:
        store_env.reset()
    assert seen["host"] == "db.internal"


def test_the_command_is_hidden_from_help():
    assert CronRunCommand().hidden is True
