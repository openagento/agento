from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.events import MigrationAppliedEvent
from agento.framework.migrate import (
    apply_migration,
    get_all_versions,
    get_applied,
    get_pending,
    migrate,
)


def _mock_conn(fetchall_return=None):
    """Create a mock pymysql Connection with cursor context manager."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = fetchall_return or []
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestGetApplied:
    def test_returns_set_of_versions(self):
        conn, cursor = _mock_conn(fetchall_return=[
            {"version": "001_create_tables"},
            {"version": "002_generalize_jobs"},
        ])

        result = get_applied(conn)

        assert result == {"001_create_tables", "002_generalize_jobs"}
        sql = cursor.execute.call_args[0][0]
        assert "schema_migration" in sql

    def test_empty_table(self):
        conn, _cursor = _mock_conn(fetchall_return=[])

        result = get_applied(conn)

        assert result == set()


class TestGetAllVersions:
    def test_discovers_sql_files(self):
        versions = get_all_versions()

        assert len(versions) >= 5
        names = [v for v, _ in versions]
        assert "001_create_tables" in names
        assert "005_agent_manager" in names

    def test_sorted_by_filename(self):
        versions = get_all_versions()
        names = [v for v, _ in versions]

        assert names == sorted(names)

    def test_paths_exist(self):
        for _, path in get_all_versions():
            assert path.exists()
            assert path.suffix == ".sql"


class TestGetPending:
    @patch("agento.framework.migrate.get_applied")
    @patch("agento.framework.migrate._ensure_migrations_table")
    def test_returns_unapplied_versions(self, mock_ensure, mock_applied):
        mock_applied.return_value = {"001_create_tables", "002_generalize_jobs"}
        conn = MagicMock()

        pending = get_pending(conn)

        versions = [v for v, _ in pending]
        assert "001_create_tables" not in versions
        assert "002_generalize_jobs" not in versions
        assert "003_rename_queued_to_todo" in versions
        assert "005_agent_manager" in versions
        mock_ensure.assert_called_once_with(conn)

    @patch("agento.framework.migrate.get_applied")
    @patch("agento.framework.migrate._ensure_migrations_table")
    def test_empty_when_all_applied(self, mock_ensure, mock_applied):
        all_versions = {v for v, _ in get_all_versions()}
        mock_applied.return_value = all_versions
        conn = MagicMock()

        pending = get_pending(conn)

        assert pending == []


class TestApplyMigration:
    def test_executes_sql_and_records_version(self, tmp_path):
        sql_file = tmp_path / "001_test.sql"
        sql_file.write_text("CREATE TABLE foo (id INT);\nCREATE TABLE bar (id INT);")
        conn, cursor = _mock_conn()

        apply_migration(conn, "001_test", sql_file)

        # 2 CREATE statements + 1 INSERT into schema_migration
        assert cursor.execute.call_count == 3
        insert_call = cursor.execute.call_args_list[-1]
        assert "schema_migration" in insert_call[0][0]
        assert insert_call[0][1] == ("001_test", "framework")
        conn.commit.assert_called_once()

    def test_strips_comments_before_splitting(self, tmp_path):
        """Comments with semicolons must not break statement splitting."""
        sql_file = tmp_path / "002_test.sql"
        sql_file.write_text(
            "-- Run manually on existing databases; 001 has the new schema.\n"
            "ALTER TABLE foo ADD COLUMN bar INT;\n"
            "ALTER TABLE foo ADD COLUMN baz INT;"
        )
        conn, cursor = _mock_conn()

        apply_migration(conn, "002_test", sql_file)

        # 2 ALTERs + 1 INSERT into schema_migration
        assert cursor.execute.call_count == 3

    def test_skips_ignorable_alter_errors(self, tmp_path):
        import pymysql.err

        sql_file = tmp_path / "003_test.sql"
        sql_file.write_text("ALTER TABLE foo ADD COLUMN bar INT;")
        conn, cursor = _mock_conn()

        # First execute raises "Duplicate column name"
        cursor.execute.side_effect = [
            pymysql.err.OperationalError(1060, "Duplicate column name 'bar'"),
            None,  # INSERT into schema_migration
        ]

        apply_migration(conn, "003_test", sql_file)

        # Still records the migration as applied
        conn.commit.assert_called_once()

    def test_raises_on_real_errors(self, tmp_path):
        import pymysql.err

        sql_file = tmp_path / "004_test.sql"
        sql_file.write_text("INVALID SQL STATEMENT;")
        conn, cursor = _mock_conn()

        cursor.execute.side_effect = pymysql.err.OperationalError(1064, "You have an error in your SQL syntax")

        with pytest.raises(pymysql.err.OperationalError):
            apply_migration(conn, "004_test", sql_file)

    def test_migration_022_idempotent_on_duplicate_column(self, tmp_path):
        """Re-running 022 after partial application must not crash."""
        import pymysql.err

        from agento.framework.migrate import apply_migration

        sql_file = tmp_path / "022_oauth_token_type_priority.sql"
        sql_file.write_text(
            "ALTER TABLE oauth_token ADD COLUMN type VARCHAR(32) NOT NULL DEFAULT 'oauth';"
        )
        conn, cursor = _mock_conn()
        cursor.execute.side_effect = [
            pymysql.err.OperationalError(1060, "Duplicate column name 'type'"),
            None,  # INSERT into schema_migration
        ]
        apply_migration(conn, "022_oauth_token_type_priority", sql_file)
        conn.commit.assert_called_once()

    def test_migration_024_uses_microsecond_used_at(self):
        sql_file = Path("src/agento/framework/sql/024_oauth_token_used_at_precision.sql")
        sql = sql_file.read_text()
        assert "MODIFY COLUMN used_at DATETIME(6) NULL" in sql
        assert "idx_oauth_token_pool_select" in sql

    def test_fresh_init_includes_toolbox_mcp_calls_column(self):
        sql_file = Path("src/agento/framework/sql/init/000_init.sql")
        sql = sql_file.read_text()
        assert "toolbox_mcp_calls INT DEFAULT NULL" in sql
        assert "('021_job_toolbox_mcp_calls')" in sql

    def test_fresh_init_includes_toolbox_mcp_connected_column(self):
        sql = Path("src/agento/framework/sql/init/000_init.sql").read_text()
        assert "toolbox_mcp_connected BOOLEAN DEFAULT NULL" in sql
        assert "('025_job_toolbox_mcp_connected')" in sql

    def test_fresh_init_includes_requester_columns(self):
        sql = Path("src/agento/framework/sql/init/000_init.sql").read_text()
        assert "requester_key   VARCHAR(255) NULL" in sql
        assert "requester_email VARCHAR(320) NULL" in sql
        assert "requester_trust VARCHAR(32) NOT NULL DEFAULT 'claimed'" in sql
        assert "requester_meta  JSON NULL" in sql
        assert "KEY idx_job_requester_key (requester_key)" in sql
        assert "KEY idx_job_requester_email (requester_email)" in sql
        assert "('026_job_requester')" in sql

    def test_fresh_init_includes_error_source_and_refresh_lease_columns(self):
        # Column alignment in the init DDL is cosmetic, so collapse runs of spaces.
        sql = re.sub(
            r"[ \t]+", " ", Path("src/agento/framework/sql/init/000_init.sql").read_text()
        )
        assert "error_source ENUM('auto','operator') NULL DEFAULT NULL" in sql
        assert "lease_owner VARCHAR(64) NULL DEFAULT NULL" in sql
        assert "leased_until DATETIME NULL DEFAULT NULL" in sql
        assert "('034_credential_error_source_and_refresh_lease')" in sql

    def test_migration_034_adds_error_source_and_refresh_lease_columns(self):
        sql = Path(
            "src/agento/framework/sql/034_credential_error_source_and_refresh_lease.sql"
        ).read_text()
        assert "ADD COLUMN error_source ENUM('auto','operator') NULL DEFAULT NULL" in sql
        assert "ADD COLUMN lease_owner VARCHAR(64) NULL DEFAULT NULL" in sql
        assert "ADD COLUMN leased_until DATETIME NULL DEFAULT NULL" in sql
        # Separate statements so each converges independently on a drifted DB — the
        # migrator splits on ';' and skips per-statement error 1060.
        assert sql.count("ALTER TABLE credential") == 3
        # NULL defaults are the "migration resurrects nothing" invariant.
        assert "NOT NULL" not in sql

    def test_migration_026_adds_requester_columns(self):
        sql = Path("src/agento/framework/sql/026_job_requester.sql").read_text()
        assert "ADD COLUMN requester_key" in sql
        assert "ADD COLUMN requester_email" in sql
        assert "ADD COLUMN requester_trust VARCHAR(32) NOT NULL DEFAULT 'claimed'" in sql
        assert "ADD COLUMN requester_meta  JSON NULL" in sql
        assert "CREATE INDEX idx_job_requester_key" in sql
        assert "CREATE INDEX idx_job_requester_email" in sql


class TestMigrate:
    @patch("agento.framework.migrate.apply_migration")
    @patch("agento.framework.migrate.get_pending")
    def test_applies_all_pending(self, mock_pending, mock_apply):
        mock_pending.return_value = [
            ("003_test", Path("/fake/003_test.sql")),
            ("004_test", Path("/fake/004_test.sql")),
        ]
        conn = MagicMock()

        applied = migrate(conn)

        assert applied == ["003_test", "004_test"]
        assert mock_apply.call_count == 2

    @patch("agento.framework.migrate.get_pending")
    def test_returns_empty_when_nothing_pending(self, mock_pending):
        mock_pending.return_value = []
        conn = MagicMock()

        applied = migrate(conn)

        assert applied == []


class _EventCollector:
    events: list = []  # noqa: RUF012

    def execute(self, event: object) -> None:
        _EventCollector.events.append(event)

    @classmethod
    def reset(cls):
        cls.events = []


class TestMigrationEvents:
    def setup_method(self):
        clear_event_manager()
        _EventCollector.reset()

    def teardown_method(self):
        clear_event_manager()

    def test_dispatches_migration_applied(self, tmp_path):
        sql_file = tmp_path / "001_test.sql"
        sql_file.write_text("CREATE TABLE foo (id INT);")
        conn, _ = _mock_conn()

        em = get_event_manager()
        em.register("migration_apply_after", ObserverEntry(name="m", observer_class=_EventCollector))

        apply_migration(conn, "001_test", sql_file)

        assert len(_EventCollector.events) == 1
        evt = _EventCollector.events[0]
        assert isinstance(evt, MigrationAppliedEvent)
        assert evt.version == "001_test"
        assert evt.module == "framework"
        assert evt.path == sql_file

    def test_migration_event_carries_module(self, tmp_path):
        sql_file = tmp_path / "001_init.sql"
        sql_file.write_text("SELECT 1;")
        conn, _ = _mock_conn()

        em = get_event_manager()
        em.register("migration_apply_after", ObserverEntry(name="m", observer_class=_EventCollector))

        apply_migration(conn, "001_init", sql_file, module="jira")

        evt = _EventCollector.events[0]
        assert evt.module == "jira"
