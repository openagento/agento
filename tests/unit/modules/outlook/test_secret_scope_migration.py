"""The DEFAULT-scope Outlook secrets migrate to workspace scope — as ciphertext.

The patch runs against a real relational store (sqlite standing in for MySQL: the four
statements it issues are plain SQL), so the row-conflict rule is asserted on actual rows
rather than on mock call arguments.
"""
from __future__ import annotations

import logging
import sqlite3
from unittest.mock import patch as mock_patch

import pytest

from agento.framework.data_patch import apply_patch
from agento.modules.outlook.src.patches.MoveSecretsToWorkspace import MoveSecretsToWorkspace

_SECRET_PATH = "outlook/outlook_client_secret"


class _Cursor:
    """Translate the patch's MySQL placeholders to sqlite's and return dict rows."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        self._raw.execute(sql.replace("%s", "?"), tuple(params))
        return self

    def fetchall(self):
        return [dict(r) for r in self._raw.fetchall()]

    def fetchone(self):
        row = self._raw.fetchone()
        return dict(row) if row is not None else None

    @property
    def rowcount(self):
        return self._raw.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Conn:
    def __init__(self, raw):
        self._raw = raw
        self.commits = 0

    def cursor(self):
        return _Cursor(self._raw.cursor())

    def commit(self):
        self.commits += 1
        self._raw.commit()


@pytest.fixture
def conn():
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.executescript(
        """
        CREATE TABLE core_config_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL, scope_id INTEGER NOT NULL,
            path TEXT NOT NULL, value TEXT, encrypted INTEGER NOT NULL DEFAULT 0,
            UNIQUE (scope, scope_id, path)
        );
        CREATE TABLE workspace (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL);
        """
    )
    return _Conn(raw)


def _add_config(conn, scope, scope_id, path, value, encrypted=1):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO core_config_data (scope, scope_id, path, value, encrypted) "
            "VALUES (%s, %s, %s, %s, %s)",
            (scope, scope_id, path, value, encrypted),
        )
    conn.commit()


def _add_workspace(conn, code):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO workspace (code) VALUES (%s)", (code,))
    conn.commit()


def _rows(conn, path=_SECRET_PATH):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope, scope_id, value, encrypted FROM core_config_data WHERE path = %s",
            (path,),
        )
        return cur.fetchall()


class TestMigration:
    def test_moves_ciphertext_to_the_single_workspace_without_decrypting(self, conn):
        _add_workspace(conn, "dev")
        _add_config(conn, "default", 0, _SECRET_PATH, "ORIGINAL_CIPHERTEXT")

        with mock_patch(
            "agento.framework.config_resolver.get_encryptor",
            side_effect=AssertionError("the patch must never decrypt"),
        ):
            changed = MoveSecretsToWorkspace().apply(conn)

        assert changed == 1
        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["scope"] == "workspace"
        assert rows[0]["scope_id"] == 1
        # The value is carried across untouched — still the same ciphertext, still flagged.
        assert rows[0]["value"] == "ORIGINAL_CIPHERTEXT"
        assert rows[0]["encrypted"] == 1

    def test_moves_every_declared_secret_path(self, conn):
        _add_workspace(conn, "dev")
        for path in (
            "outlook/outlook_client_secret",
            "outlook/outlook_cert_pem",
            "outlook/outlook_cert_password",
        ):
            _add_config(conn, "default", 0, path, f"CT-{path}")

        assert MoveSecretsToWorkspace().apply(conn) == 3
        for path in ("outlook/outlook_cert_pem", "outlook/outlook_cert_password"):
            assert _rows(conn, path)[0]["scope"] == "workspace"

    def test_it_leaves_non_secret_outlook_rows_alone(self, conn):
        _add_workspace(conn, "dev")
        _add_config(conn, "default", 0, _SECRET_PATH, "CT")
        _add_config(conn, "default", 0, "outlook/poll_top", "10", encrypted=0)

        MoveSecretsToWorkspace().apply(conn)
        assert _rows(conn, "outlook/poll_top")[0]["scope"] == "default"

    def test_an_existing_workspace_row_wins_and_the_default_row_is_deleted(self, conn):
        _add_workspace(conn, "dev")
        _add_config(conn, "default", 0, _SECRET_PATH, "DEFAULT_CIPHERTEXT")
        _add_config(conn, "workspace", 1, _SECRET_PATH, "WORKSPACE_CIPHERTEXT")

        MoveSecretsToWorkspace().apply(conn)

        rows = _rows(conn)
        assert len(rows) == 1
        assert rows[0]["scope"] == "workspace"
        assert rows[0]["value"] == "WORKSPACE_CIPHERTEXT"

    def test_it_aborts_when_the_target_workspace_is_ambiguous(self, conn):
        _add_workspace(conn, "dev")
        _add_workspace(conn, "prod")
        _add_config(conn, "default", 0, _SECRET_PATH, "CT")

        with pytest.raises(RuntimeError) as ei:
            MoveSecretsToWorkspace().apply(conn)

        message = str(ei.value)
        # The operator must be able to act on the message alone.
        assert "dev, prod" in message
        assert "config:set" in message
        assert "config:remove" in message
        # FAIL-CLOSED: nothing moved, so a re-run after remediation is still correct.
        assert _rows(conn)[0]["scope"] == "default"

    def test_it_aborts_when_there_is_no_workspace_at_all(self, conn):
        _add_config(conn, "default", 0, _SECRET_PATH, "CT")
        with pytest.raises(RuntimeError) as ei:
            MoveSecretsToWorkspace().apply(conn)
        assert "(none)" in str(ei.value)

    def test_it_is_a_noop_when_no_default_rows_exist(self, conn):
        _add_workspace(conn, "dev")
        assert MoveSecretsToWorkspace().apply(conn) == 0
        assert conn.commits == 1  # only the fixture's own insert

    def test_it_is_idempotent(self, conn):
        _add_workspace(conn, "dev")
        _add_config(conn, "default", 0, _SECRET_PATH, "CT")
        assert MoveSecretsToWorkspace().apply(conn) == 1
        assert MoveSecretsToWorkspace().apply(conn) == 0

    def test_require_declares_no_dependencies(self):
        assert MoveSecretsToWorkspace().require() == []


class TestThroughTheRealExecutor:
    def test_the_declared_class_path_resolves_and_runs(self, conn, tmp_path):
        """`apply_patch` imports the class named in data_patch.json — a typo there is
        invisible until deploy, so resolve it the same way setup:upgrade does."""
        from pathlib import Path
        from types import SimpleNamespace

        _add_workspace(conn, "dev")
        _add_config(conn, "default", 0, _SECRET_PATH, "CT")

        manifest = SimpleNamespace(
            name="outlook",
            path=Path("src/agento/modules/outlook").resolve(),
        )
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE data_patch (name TEXT, module TEXT)")

        apply_patch(
            manifest,
            {
                "name": "MoveSecretsToWorkspace",
                "class": "src.patches.MoveSecretsToWorkspace.MoveSecretsToWorkspace",
            },
            conn,
            logging.getLogger("test"),
        )

        assert _rows(conn)[0]["scope"] == "workspace"
