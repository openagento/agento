"""E1.5 framework tables: the migrated schema accepts and rejects the shared fixture's rows,
and a fresh install from 000_init.sql builds the same tables."""
from __future__ import annotations

import json
from pathlib import Path

import pymysql
import pytest

import agento.framework as fw
from agento.framework.migrate import apply_migration

from .conftest import TEST_DB, _load_fixture, _root_connection, _test_connection

ROWS = _load_fixture("auth_context_v1.json")["table_rows"]
TABLES = ("user", "session", "launch", "role_grant")
MISSING_ID = 999_999_999


@pytest.fixture
def scope():
    conn = _test_connection(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO workspace (code, label) VALUES ('e15-ws', 'e15')")
        workspace_id = cur.lastrowid
        cur.execute(
            "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, 'e15-av', 'e15')",
            (workspace_id,),
        )
        agent_view_id = cur.lastrowid
        cur.execute("INSERT INTO `user` (username, role) VALUES ('e15-owner', 'user')")
        user_id = cur.lastrowid
    yield conn, {"@user": user_id, "@workspace": workspace_id,
                 "@agent_view": agent_view_id, "@missing": MISSING_ID}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workspace WHERE id = %s", (workspace_id,))
        cur.execute("DELETE FROM `user`")
        cur.execute("DELETE FROM role_grant")
    conn.close()


def _insert(cur, table, row, refs):
    values = []
    for v in row.values():
        if isinstance(v, str) and v.startswith("@"):
            v = refs[v]
        elif isinstance(v, list):
            v = json.dumps(v)
        values.append(v)
    columns = ", ".join(f"`{c}`" for c in row)
    marks = ", ".join(["%s"] * len(row))
    cur.execute(f"INSERT INTO `{table}` ({columns}) VALUES ({marks})", values)


@pytest.mark.parametrize(
    "table,case",
    [(t, c) for t in TABLES for c in ROWS[t]],
    ids=[f"{t}: {c['why']}" for t in TABLES for c in ROWS[t]],
)
def test_fixture_row(scope, table, case):
    conn, refs = scope
    with conn.cursor() as cur:
        if case["valid"]:
            _insert(cur, table, case["row"], refs)
        else:
            with pytest.raises((pymysql.IntegrityError, pymysql.OperationalError)):
                _insert(cur, table, case["row"], refs)


def test_deleting_a_user_takes_its_sessions_and_launches(scope):
    conn, refs = scope
    with conn.cursor() as cur:
        _insert(cur, "session", ROWS["session"][0]["row"], refs)
        _insert(cur, "launch", ROWS["launch"][0]["row"], refs)
        cur.execute("DELETE FROM `user` WHERE id = %s", (refs["@user"],))
        cur.execute("SELECT (SELECT COUNT(*) FROM session) + (SELECT COUNT(*) FROM launch) AS n")
        assert cur.fetchone()["n"] == 0


def test_deleting_an_agent_view_takes_its_launches_and_grants(scope):
    conn, refs = scope
    with conn.cursor() as cur:
        _insert(cur, "launch", ROWS["launch"][0]["row"], refs)
        _insert(cur, "role_grant", ROWS["role_grant"][0]["row"], refs)
        cur.execute("DELETE FROM agent_view WHERE id = %s", (refs["@agent_view"],))
        cur.execute("SELECT (SELECT COUNT(*) FROM launch) + (SELECT COUNT(*) FROM role_grant) AS n")
        assert cur.fetchone()["n"] == 0


def test_capability_and_audit_take_a_string_version_id():
    version = ROWS["launch"][0]["row"]["version_id"]
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO toolbox_capability (token_hash, kind, expires_at, app_version_id) "
                "VALUES (%s, 'miniapp', NOW() + INTERVAL 1 MINUTE, %s)", ("9" * 64, version))
            cur.execute(
                "INSERT INTO tool_invocation (execution_id, transport, tool_name, args_sha256, "
                "app_version_id, outcome) VALUES (UUID(), 'invoke', 't', %s, %s, 'pending')",
                ("0" * 64, version))
            cur.execute("SELECT app_version_id FROM toolbox_capability WHERE token_hash = %s", ("9" * 64,))
            assert cur.fetchone()["app_version_id"] == version
            cur.execute("DELETE FROM tool_invocation WHERE app_version_id = %s", (version,))
    finally:
        conn.close()


def _columns(database):
    conn = _root_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
                "AND TABLE_NAME IN ('user', 'session', 'launch', 'role_grant', "
                "'toolbox_capability', 'tool_invocation') ORDER BY TABLE_NAME, ORDINAL_POSITION",
                (database,))
            columns = cur.fetchall()
            cur.execute(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE FROM "
                "information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA = %s "
                "ORDER BY TABLE_NAME, CONSTRAINT_NAME", (database,))
            constraints = [c for c in cur.fetchall() if c[0] in TABLES]
        return columns, constraints
    finally:
        conn.close()


def test_fresh_install_matches_the_migrated_schema():
    fresh = f"{TEST_DB}_init"
    init = Path(fw.__file__).parent / "sql" / "init" / "000_init.sql"
    root = _root_connection()
    try:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {fresh}")
            cur.execute(f"CREATE DATABASE {fresh} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        root.select_db(fresh)
        apply_migration(root, "000_init", init)
        assert _columns(fresh) == _columns(TEST_DB)
    finally:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {fresh}")
        root.close()
