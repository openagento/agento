"""WS8: least-privilege DB users. setup:upgrade regenerates the runtime users' grants."""
from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from agento.framework import db_grants
from agento.framework.setup import SetupResult


def _conn(tables):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [{"table_name": t} for t in tables]
    return conn


def _statements(conn):
    cur = conn.cursor.return_value.__enter__.return_value
    return [(c.args[0], tuple(c.args[1]) if len(c.args) > 1 else ()) for c in cur.execute.call_args_list]


def _apply(tables=("job", "credential", "agent_view", "acme_new_table")):
    conn = _conn(tables)
    db_grants.apply_grants(
        conn, backend_user="agento_backend", backend_password="b-pw", toolbox_password="t-pw",
    )
    return _statements(conn)


def _grants_to(statements, user):
    return [sql for sql, params in statements if sql.startswith("GRANT") and params == (user,)]


class TestToolboxAllowList:
    def test_the_toolbox_grant_is_exactly_what_its_sql_does(self):
        # Pinned: no grant on credential or run state; a new table gets none (deny by default).
        assert db_grants.TOOLBOX_GRANTS == {
            "agent_view": "SELECT",
            "core_config_data": "SELECT",
            "launch": "SELECT",
            "role_grant": "SELECT",
            "session": "SELECT",
            "user": "SELECT",
            "workspace": "SELECT",
            "job": "SELECT, INSERT",
            "miniapp_activation": "SELECT, INSERT, UPDATE, DELETE",
            "versioned_artifact": "SELECT, INSERT, UPDATE, DELETE",
            "versioned_artifact_audit": "INSERT",
            "tool_invocation": "SELECT, INSERT, UPDATE",
            "toolbox_capability": "SELECT, UPDATE",
        }


class TestApplyGrants:
    def test_toolbox_gets_grants_only_on_tables_that_exist(self):
        toolbox = _grants_to(_apply(), db_grants.TOOLBOX_USER)
        assert toolbox == [
            "GRANT SELECT, INSERT ON `job` TO %s@'%%'",
            "GRANT SELECT ON `agent_view` TO %s@'%%'",
        ]

    def test_backend_gets_dml_on_the_database_and_nothing_more(self):
        backend = _grants_to(_apply(), "agento_backend")
        # `ON *` = the default database: every table, also one a later upgrade adds. No DDL.
        assert backend == ["GRANT SELECT, INSERT, UPDATE, DELETE ON * TO %s@'%%'"]

    def test_each_user_is_synced_then_reset_then_granted(self):
        statements = _apply()
        for user, password in (("agento_backend", "b-pw"), (db_grants.TOOLBOX_USER, "t-pw")):
            mine = [sql for sql, params in statements if params and params[0] == user]
            assert mine[0] == "CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s"
            assert mine[1] == "ALTER USER %s@'%%' IDENTIFIED BY %s"
            assert mine[2] == "REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@'%%'"
            assert all(sql.startswith("GRANT ") for sql in mine[3:])
            # The password is a bound parameter, never part of the SQL text (SEC-5, SEC-6).
            assert (user, password) in [params for _, params in statements]
            assert not any(password in sql for sql, _ in statements)

    @pytest.mark.parametrize("kwargs", [
        {"backend_password": ""},
        {"toolbox_password": ""},
        {"backend_user": ""},
        {"backend_user": db_grants.MIGRATE_USER},
        {"backend_user": db_grants.TOOLBOX_USER},
    ])
    def test_fails_closed_before_any_statement(self, kwargs):
        conn = _conn(["job"])
        args = {"backend_user": "agento_backend", "backend_password": "b", "toolbox_password": "t"}
        with pytest.raises(ValueError):
            db_grants.apply_grants(conn, **{**args, **kwargs})
        assert _statements(conn) == []


class TestSetupUpgradeCommand:
    def _run(self, monkeypatch, *, dry_run=False, migrate_password="m-pw"):
        from agento.framework.cli.runtime import SetupUpgradeCommand

        for key, value in {
            "MYSQL_USER": "agento_backend", "MYSQL_PASSWORD": "b-pw",
            "MYSQL_TOOLBOX_PASSWORD": "t-pw",
        }.items():
            monkeypatch.setenv(key, value)
        if migrate_password:
            monkeypatch.setenv("MYSQL_MIGRATE_PASSWORD", migrate_password)
        else:
            monkeypatch.delenv("MYSQL_MIGRATE_PASSWORD", raising=False)
        conn = MagicMock()
        with patch("agento.framework.cli.runtime.get_connection_or_exit", return_value=conn) as connect, \
             patch("agento.framework.setup.setup_upgrade", return_value=SetupResult()), \
             patch("agento.framework.db_grants.apply_grants") as grants:
            SetupUpgradeCommand().execute(Namespace(dry_run=dry_run, skip_onboarding=True))
        return connect.call_args.args[0], grants

    def test_connects_as_the_migration_user_and_regenerates_grants(self, monkeypatch):
        config, grants = self._run(monkeypatch)
        assert (config.mysql_user, config.mysql_password) == (db_grants.MIGRATE_USER, "m-pw")
        grants.assert_called_once()
        assert grants.call_args.kwargs == {
            "backend_user": "agento_backend", "backend_password": "b-pw", "toolbox_password": "t-pw",
        }

    def test_dry_run_changes_no_grant(self, monkeypatch):
        _, grants = self._run(monkeypatch, dry_run=True)
        grants.assert_not_called()

    def test_one_user_setup_keeps_the_runtime_user_and_skips_grants(self, monkeypatch):
        # A host-side dev run or CI has no migration user: it stays one user, as before.
        config, grants = self._run(monkeypatch, migrate_password="")
        assert config.mysql_user == "agento_backend"
        grants.assert_not_called()
