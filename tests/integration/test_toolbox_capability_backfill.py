"""Migration 036 backfills pre-E1 capability rows so the auth context v1 verifier accepts them.

A fresh test database already carries 036, so the legacy rows are inserted with every new
column NULL — exactly what a pre-E1 row looks like after the ALTERs — and the migration file is
applied once more under a throw-away version: its ALTERs are skipped (duplicate column / key),
its backfill UPDATEs run for real.
"""
from __future__ import annotations

import json
from pathlib import Path

import agento.framework as fw
from agento.framework.auth_context import LEGACY_REST_SUBJECT, derive_auth_context
from agento.framework.migrate import apply_migration

from .conftest import _test_connection

_MIGRATION = Path(fw.__file__).parent / "sql" / "036_toolbox_capability_auth_context.sql"
_RETEST_VERSION = "036_toolbox_capability_auth_context_retest"


def _legacy_row(cur, token_hash, kind, agent_view_id, job_id, hours):
    cur.execute(
        "INSERT INTO toolbox_capability (token_hash, kind, agent_view_id, job_id, expires_at) "
        "VALUES (%s, %s, %s, %s, NOW() + INTERVAL %s HOUR)",
        (token_hash, kind, agent_view_id, job_id, hours),
    )


def _as_verifier_row(row):
    out = dict(row)
    out["job_id"] = None if row["job_id"] is None else str(row["job_id"])
    out["allowed_transports"] = json.loads(row["allowed_transports"])
    out["created_at"] = int(row["created_at_epoch"])
    out["expires_at"] = int(row["expires_at_epoch"])
    return out


def test_036_backfills_legacy_rows_per_kind(int_agent_view):
    view_id = int_agent_view
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM agent_view WHERE id = %s", (view_id,))
            workspace_id = cur.fetchone()["workspace_id"]
            _legacy_row(cur, "a" * 64, "mcp_job", view_id, 42, 24)
            _legacy_row(cur, "b" * 64, "mcp_interactive", view_id, None, 1)
            _legacy_row(cur, "c" * 64, "internal_rest", view_id, 43, 24)
            _legacy_row(cur, "d" * 64, "internal_rest", None, None, 24)
        try:
            apply_migration(conn, _RETEST_VERSION, _MIGRATION)
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM schema_migration WHERE version = %s", (_RETEST_VERSION,))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT *, UNIX_TIMESTAMP(created_at) AS created_at_epoch, "
                "UNIX_TIMESTAMP(expires_at) AS expires_at_epoch, "
                "TIMESTAMPDIFF(SECOND, NOW(), expires_at) AS remaining "
                "FROM toolbox_capability ORDER BY token_hash"
            )
            job, interactive, rest, viewless = cur.fetchall()
    finally:
        conn.close()

    for row in (job, interactive):
        assert row["actor"] == "agent"
        assert row["subject_id"] == str(view_id)
        assert json.loads(row["allowed_transports"]) == ["sse", "http"]
        assert row["workspace_id"] == workspace_id
    # The 24 h token is cut to 4 h; the 1 h one keeps its own, shorter clock.
    assert 4 * 3600 - 60 <= job["remaining"] <= 4 * 3600
    assert interactive["remaining"] <= 3600

    for row in (rest, viewless):
        assert row["actor"] == "service"
        assert row["subject_id"] == LEGACY_REST_SUBJECT
        assert json.loads(row["allowed_transports"]) == ["http"]
        assert row["remaining"] <= 4 * 3600
    assert rest["workspace_id"] == workspace_id
    assert viewless["workspace_id"] is None

    for row, endpoints in (
        (job, ("mcp", "sse", "messages")),
        (interactive, ("mcp", "sse")),
        (rest, ("api", "health", "config_test")),
        (viewless, ("config_test",)),
    ):
        for endpoint in endpoints:
            assert derive_auth_context(
                row=_as_verifier_row(row), agent_view_workspace_id=workspace_id,
                endpoint=endpoint,
            ), (row["kind"], endpoint)
