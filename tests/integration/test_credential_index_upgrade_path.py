"""The credential schema must end up the same whether you install fresh or upgrade.

Round 5 replaced the global `UNIQUE(label)` with `UNIQUE(scope, label)` — but only on fresh
installs. Migration 005 created the index as ``uq_oauth_tokens_label`` (PLURAL, matching the
then-plural table), migration 013 renamed only the TABLE (MySQL keeps index names across
``RENAME TABLE``), and migrations 030/032 dropped the SINGULAR spellings. Because
``migrate.py`` swallows error 1091 ("can't DROP; check that it exists"), those DROPs became
silent no-ops on upgraded databases while still being recorded as applied — so the
credential-overwriting bug stayed live exactly where real data lives.

Source-text assertions cannot catch that: the migration files look correct in isolation. Only
running the real migration chain against a real MySQL does.
"""
from __future__ import annotations

import pymysql
import pytest

from agento.framework.migrate import migrate

from .conftest import TEST_DB, _root_connection, _test_connection

UPGRADE_DB = f"{TEST_DB}_upgrade"
SQL_DIR = None  # resolved lazily from the framework package


def _sql_dir():
    from pathlib import Path

    import agento.framework as fw

    return Path(fw.__file__).parent / "sql"


def _connect(db: str, autocommit: bool = True) -> pymysql.Connection:
    base = _test_connection(autocommit=autocommit)
    cfg = base
    cfg.close()
    conn = pymysql.connect(
        host="localhost", port=3306, user="root", password="cronagent_root",
        database=db, autocommit=autocommit, cursorclass=pymysql.cursors.DictCursor,
    )
    return conn


@pytest.fixture
def upgraded_db():
    """A database built the way a real deployment was: 005 onward, in order."""
    root = _root_connection()
    try:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {UPGRADE_DB}")
            cur.execute(
                f"CREATE DATABASE {UPGRADE_DB} "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        root.close()

    conn = _connect(UPGRADE_DB, autocommit=False)
    try:
        migrate(conn)
        conn.commit()
    finally:
        conn.close()

    yield UPGRADE_DB

    root = _root_connection()
    try:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {UPGRADE_DB}")
    finally:
        root.close()


def _indexes(db: str, table: str) -> dict[str, list[str]]:
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW INDEX FROM {table}")
            rows = cur.fetchall()
    finally:
        conn.close()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["Key_name"], []).append(r["Column_name"])
    return out


class TestCredentialIndexesAfterUpgrade:
    def test_no_global_label_unique_key_survives(self, upgraded_db):
        """Either spelling of the historical global key is a live cross-scope overwrite."""
        keys = _indexes(upgraded_db, "credential")

        for dead in ("uq_oauth_tokens_label", "uq_oauth_token_label"):
            assert dead not in keys, (
                f"{dead} survived the upgrade — cross-scope label reuse still overwrites. "
                f"Present: {sorted(keys)}"
            )

    def test_scoped_unique_key_exists(self, upgraded_db):
        keys = _indexes(upgraded_db, "credential")

        assert "uq_credential_scope_label" in keys, sorted(keys)
        assert keys["uq_credential_scope_label"] == ["scope", "label"]

    def test_no_plural_legacy_index_survives(self, upgraded_db):
        """Migration 030 had the same plural/singular mismatch for the pool index."""
        keys = _indexes(upgraded_db, "credential")

        assert "idx_oauth_tokens_agent_enabled" not in keys, sorted(keys)
        assert "idx_oauth_token_agent_enabled" not in keys, sorted(keys)

    def test_upgrade_matches_fresh_install(self, upgraded_db):
        """Fresh/upgrade parity on the constraint that matters, so the two paths cannot
        diverge again."""
        upgraded = _indexes(upgraded_db, "credential")
        fresh = _indexes(TEST_DB, "credential")

        def unique_label_keys(keys):
            return {
                name: cols for name, cols in keys.items()
                if "label" in cols
            }

        assert unique_label_keys(upgraded) == unique_label_keys(fresh)


class TestLabelReuseAcrossScopes:
    """The behaviour the constraint exists for: one label, two scopes, two credentials."""

    def test_same_label_in_two_scopes_yields_two_isolated_rows(self, upgraded_db):
        from agento.framework.agent_manager.credential_store import register_credential

        conn = _connect(upgraded_db, autocommit=False)
        try:
            first = register_credential(
                conn, scope="claude", label="my-token",
                credentials={"subscription_key": "claude-secret"},
            )
            second = register_credential(
                conn, scope="codex", label="my-token",
                credentials={"subscription_key": "codex-secret"},
            )
            conn.commit()

            assert first.id != second.id, (
                "the second registration UPDATED the first row instead of inserting"
            )
            assert (first.scope, second.scope) == ("claude", "codex")

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT scope, label FROM credential WHERE label = %s ORDER BY scope",
                    ("my-token",),
                )
                rows = cur.fetchall()
            assert [(r["scope"], r["label"]) for r in rows] == [
                ("claude", "my-token"), ("codex", "my-token"),
            ]
        finally:
            conn.close()

    def test_first_scope_credentials_are_not_overwritten(self, upgraded_db):
        """The precise damage: the surviving row used to hold the SECOND scope's secret."""
        from agento.framework.agent_manager.credential_store import (
            get_credential,
            register_credential,
        )

        conn = _connect(upgraded_db, autocommit=False)
        try:
            first = register_credential(
                conn, scope="claude", label="shared",
                credentials={"subscription_key": "claude-secret"},
            )
            register_credential(
                conn, scope="codex", label="shared",
                credentials={"subscription_key": "codex-secret"},
            )
            conn.commit()

            reloaded = get_credential(conn, first.id)
            assert reloaded.scope == "claude"
            assert reloaded.credentials["subscription_key"] == "claude-secret"
        finally:
            conn.close()

    def test_same_label_same_scope_still_upserts(self, upgraded_db):
        """Re-registering in the SAME scope must keep updating in place — that is the
        documented recovery path, not a new row."""
        from agento.framework.agent_manager.credential_store import register_credential

        conn = _connect(upgraded_db, autocommit=False)
        try:
            first = register_credential(
                conn, scope="claude", label="same",
                credentials={"subscription_key": "v1"},
            )
            again = register_credential(
                conn, scope="claude", label="same",
                credentials={"subscription_key": "v2"},
            )
            conn.commit()

            assert first.id == again.id
            assert again.credentials["subscription_key"] == "v2"
        finally:
            conn.close()
