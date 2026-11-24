"""`tool:sync-catalog` and the best-effort catalog sync after a module toggle."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.cli import module as mod
from agento.framework.cli.tool_catalog import ToolSyncCatalogCommand
from agento.framework.tool_catalog import SyncResult


def test_command_syncs_and_closes(capsys):
    conn = MagicMock()
    with (
        patch("agento.framework.cli.tool_catalog._load_framework_config", return_value=("db", None, None)),
        patch("agento.framework.cli.tool_catalog.get_connection_or_exit", return_value=conn),
        patch("agento.framework.tool_catalog.sync_enabled_modules", return_value=SyncResult(2, 1, 3)) as sync,
    ):
        ToolSyncCatalogCommand().execute(MagicMock())
    sync.assert_called_once_with(conn)
    conn.close.assert_called_once()
    assert "2 new, 1 changed, 3 absent" in capsys.readouterr().out


def test_command_closes_on_failure():
    conn = MagicMock()
    with (
        patch("agento.framework.cli.tool_catalog._load_framework_config", return_value=("db", None, None)),
        patch("agento.framework.cli.tool_catalog.get_connection_or_exit", return_value=conn),
        patch("agento.framework.tool_catalog.sync_enabled_modules", side_effect=RuntimeError("x")),
        pytest.raises(RuntimeError),
    ):
        ToolSyncCatalogCommand().execute(MagicMock())
    conn.close.assert_called_once()


def test_host_toggle_failure_only_warns(tmp_path, capsys):
    with (
        patch("agento.framework.cli._project.compose_file_flags", return_value=["-f", "c.yml"]),
        patch.object(mod.subprocess, "run", return_value=MagicMock(returncode=1)),
    ):
        mod._sync_tool_catalog(tmp_path)
    assert "cron sync will do it" in capsys.readouterr().out


def test_container_toggle_syncs_directly(tmp_path):
    conn = MagicMock()
    with (
        patch("agento.framework.cli.runtime._load_framework_config", return_value=("db", None, None)),
        patch("agento.framework.db.get_connection", return_value=conn),
        patch("agento.framework.tool_catalog.sync_enabled_modules") as sync,
    ):
        mod._sync_tool_catalog(None, core_dir=Path("/c"), user_dir=Path("/u"))
    sync.assert_called_once_with(conn, core_dir="/c", user_dir="/u")
    conn.close.assert_called_once()


def test_container_toggle_without_db_only_warns(capsys):
    with (
        patch("agento.framework.cli.runtime._load_framework_config", return_value=("db", None, None)),
        patch("agento.framework.db.get_connection", side_effect=OSError("no db")),
    ):
        mod._sync_tool_catalog(None, core_dir=Path("/c"), user_dir=Path("/u"))
    assert "cron sync will do it" in capsys.readouterr().out
