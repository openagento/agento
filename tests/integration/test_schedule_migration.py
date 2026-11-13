"""056_generic_schedule ends in one schema whatever ran before it (AG-67 plan §4.1).

Path (a): the old shape without the jira_periodic_tasks migration, then 056, then that one.
Path (b): the old shape with it, then 056. Both convert a legacy row, and the module
migration that runs after 056 hits only the errors the runner skips.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import agento.framework as fw
from agento.framework.migrate import migrate

from .conftest import TEST_DB, _db_connection, _root_connection

FW_SQL = Path(fw.__file__).parent / "sql"
JIRA_SQL = FW_SQL.parent.parent / "modules" / "jira_periodic_tasks" / "sql"


def _fresh(db: str) -> None:
    root = _root_connection()
    try:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
            cur.execute(f"CREATE DATABASE {db} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        root.close()


def _drop(db: str) -> None:
    root = _root_connection()
    try:
        with root.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
    finally:
        root.close()


def _old_shape(conn, tmp_path: Path) -> None:
    """Every framework migration before 056, as a deployment on the previous release has."""
    old = tmp_path / "old"
    old.mkdir(parents=True)
    for f in FW_SQL.glob("*.sql"):
        if f.stem < "056":
            shutil.copy(f, old / f.name)
    migrate(conn, sql_dir=old)


def _jira(conn) -> None:
    migrate(conn, module="jira_periodic_tasks", sql_dir=JIRA_SQL)


def _legacy_row(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (issue_key, summary, agent_type, cron_expr) "
                    "VALUES ('AI-1', 'Weekly report', 'cron', '0 7 * * 1')")
    conn.commit()


def _schema(conn, db: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT column_name AS c, column_type AS t, is_nullable AS n, column_default AS d "
                    "FROM information_schema.columns WHERE table_schema=%s AND table_name='schedule' "
                    "ORDER BY column_name", (db,))
        columns = [tuple(r.values()) for r in cur.fetchall()]
        cur.execute("SELECT index_name AS i, non_unique AS u, "
                    "GROUP_CONCAT(column_name ORDER BY seq_in_index) AS c FROM information_schema.statistics "
                    "WHERE table_schema=%s AND table_name='schedule' GROUP BY index_name, non_unique "
                    "ORDER BY index_name", (db,))
        indexes = [tuple(r.values()) for r in cur.fetchall()]
        cur.execute("SELECT k.constraint_name AS n, k.column_name AS c, k.referenced_table_name AS t, "
                    "r.delete_rule AS d FROM information_schema.key_column_usage k "
                    "JOIN information_schema.referential_constraints r "
                    "ON r.constraint_schema = k.constraint_schema AND r.constraint_name = k.constraint_name "
                    "WHERE k.table_schema=%s AND k.table_name='schedule' ORDER BY k.constraint_name", (db,))
        fks = [tuple(r.values()) for r in cur.fetchall()]
    return {"columns": columns, "indexes": indexes, "fks": fks}


@pytest.fixture
def two_dbs(tmp_path):
    dbs = (f"{TEST_DB}_sched_a", f"{TEST_DB}_sched_b")
    for db in dbs:
        _fresh(db)
    yield dbs
    for db in dbs:
        _drop(db)


def test_both_paths_end_in_one_schema_and_convert_the_legacy_row(two_dbs, tmp_path):
    db_a, db_b = two_dbs
    a = _db_connection(db_a)
    b = _db_connection(db_b)
    try:
        _old_shape(a, tmp_path / "a")
        _legacy_row(a)
        assert migrate(a) == ["056_generic_schedule"]
        _jira(a)

        _old_shape(b, tmp_path / "b")
        _legacy_row(b)
        _jira(b)
        assert migrate(b) == ["056_generic_schedule"]

        schema_a, schema_b = _schema(a, db_a), _schema(b, db_b)
        assert schema_a == schema_b
        index_names = {i[0] for i in schema_a["indexes"]}
        assert "uq_schedule_agent_view_issue" in index_names
        assert not index_names & {"uq_schedule_issue", "uq_schedules_issue"}  # no global unique key
        assert {c[0] for c in schema_a["columns"]} >= {
            "source", "source_ref", "title", "rrule", "starts_at", "timezone", "next_fire_at",
            "anchor_local", "owner_user_id", "owner_agent_view_id", "deleted_at", "created_job_id"}

        for conn in (a, b):
            with conn.cursor() as cur:
                cur.execute("SELECT source, source_ref, title, cron_expr, enabled, next_fire_at FROM schedule")
                assert cur.fetchall() == [{"source": "cron", "source_ref": "AI-1", "title": "Weekly report",
                                           "cron_expr": "0 7 * * 1", "enabled": 1, "next_fire_at": None}]
    finally:
        a.close()
        b.close()


def test_two_views_may_hold_the_same_source_ref(two_dbs, tmp_path):
    db_a, _ = two_dbs
    conn = _db_connection(db_a)
    try:
        _old_shape(conn, tmp_path)
        migrate(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES ('w', 'w')")
            ws = cur.lastrowid
            ids = []
            for code in ("v1", "v2"):
                cur.execute("INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, %s, %s)", (ws, code, code))
                ids.append(cur.lastrowid)
            for view in ids:
                cur.execute("INSERT INTO schedule (source, source_ref, agent_view_id) VALUES ('x', 'AI-1', %s)", (view,))
            cur.execute("INSERT INTO schedule (source) VALUES ('scheduler'), ('scheduler')")  # NULL refs
        conn.commit()
    finally:
        conn.close()
