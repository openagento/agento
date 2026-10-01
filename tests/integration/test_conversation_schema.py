"""The six `conversation` tables of PRD E3-E5 §3.2, against the real migrated schema.

Every assertion here is about a rule the DDL states and the application must not have to
restate: which FK survives a deleted parent, which column deliberately has NO FK, and the
CHECK that keeps job bookkeeping off an assistant row.
"""
from __future__ import annotations

import pymysql
import pytest

from .conftest import TEST_DB, _root_connection, _test_connection

TABLES = (
    "conversation",
    "message",
    "execution",
    "conversation_event",
    "execution_delta",
    "conversation_prune_watermark",
)


@pytest.fixture
def scope():
    """A workspace / agent_view / user / conversation to hang rows off."""
    conn = _test_connection(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO workspace (code, label) VALUES ('conv-ws', 'conv')")
        workspace_id = cur.lastrowid
        cur.execute(
            "INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, 'conv-av', 'conv')",
            (workspace_id,),
        )
        agent_view_id = cur.lastrowid
        cur.execute("INSERT INTO `user` (username, role) VALUES ('conv-owner', 'user')")
        user_id = cur.lastrowid
        cur.execute(
            "INSERT INTO conversation (agent_view_id, user_id, title) VALUES (%s, %s, 't')",
            (agent_view_id, user_id),
        )
        conversation_id = cur.lastrowid
    yield conn, {"agent_view": agent_view_id, "user": user_id, "conversation": conversation_id}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM workspace WHERE id = %s", (workspace_id,))
        cur.execute("DELETE FROM `user` WHERE id = %s", (user_id,))
    conn.close()


def _rows(sql, params=()):
    conn = _root_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("table", TABLES)
def test_the_table_exists(table):
    assert _rows(
        "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (TEST_DB, table),
    )


@pytest.mark.parametrize(
    "table,columns",
    [
        ("conversation", "user_id,status,updated_at"),
        ("message", "conversation_id,id"),
        ("message", "job_state,created_at"),
        ("conversation_event", "conversation_id,id"),
        ("conversation_event", "created_at"),
        ("execution", "job_id,attempt"),
    ],
)
def test_the_index_exists(table, columns):
    found = {
        (r[0], r[1])
        for r in _rows(
            "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) FROM "
            "information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "GROUP BY INDEX_NAME",
            (TEST_DB, table),
        )
    }
    assert columns in {c for _, c in found}, found


@pytest.mark.parametrize(
    "table,column,action",
    [
        ("conversation", "agent_view_id", "SET NULL"),
        ("conversation", "user_id", "RESTRICT"),
        ("message", "conversation_id", "CASCADE"),
        ("conversation_event", "conversation_id", "CASCADE"),
        ("conversation_prune_watermark", "conversation_id", "CASCADE"),
    ],
)
def test_each_foreign_key_states_its_on_delete(table, column, action):
    rows = _rows(
        "SELECT r.DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS r "
        "JOIN information_schema.KEY_COLUMN_USAGE k ON k.CONSTRAINT_NAME = r.CONSTRAINT_NAME "
        "AND k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA "
        "WHERE r.CONSTRAINT_SCHEMA = %s AND r.TABLE_NAME = %s AND k.COLUMN_NAME = %s",
        (TEST_DB, table, column),
    )
    assert [r[0] for r in rows] == [action]


@pytest.mark.parametrize(
    "table,column",
    [("message", "job_id"), ("execution", "job_id"), ("execution_delta", "execution_id")],
)
def test_the_column_deliberately_has_no_foreign_key(table, column):
    """A job is pruned on its own schedule; a message and an execution outlive it."""
    assert not _rows(
        "SELECT 1 FROM information_schema.KEY_COLUMN_USAGE WHERE CONSTRAINT_SCHEMA = %s "
        "AND TABLE_NAME = %s AND COLUMN_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL",
        (TEST_DB, table, column),
    )


def test_two_assistant_rows_with_no_client_message_id_coexist(scope):
    """The UNIQUE key must not make NULL the one client_message_id per conversation."""
    conn, refs = scope
    with conn.cursor() as cur:
        for _ in range(2):
            cur.execute(
                "INSERT INTO message (conversation_id, role, content) VALUES (%s, 'assistant', 'x')",
                (refs["conversation"],),
            )
        cur.execute(
            "SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (refs["conversation"],)
        )
        assert cur.fetchone()["n"] == 2


def test_job_state_is_refused_on_an_assistant_row(scope):
    """Job bookkeeping belongs to the user turn that started the job."""
    conn, refs = scope
    with conn.cursor() as cur, pytest.raises(pymysql.err.OperationalError):
        cur.execute(
            "INSERT INTO message (conversation_id, role, content, job_state) "
            "VALUES (%s, 'assistant', 'x', 'pending')",
            (refs["conversation"],),
        )


def test_job_state_is_allowed_on_a_user_row(scope):
    conn, refs = scope
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message (conversation_id, role, content, job_state) "
            "VALUES (%s, 'user', 'x', 'pending')",
            (refs["conversation"],),
        )


def test_deleting_an_agent_view_leaves_its_conversations_readable(scope):
    conn, refs = scope
    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_view WHERE id = %s", (refs["agent_view"],))
        cur.execute("SELECT agent_view_id FROM conversation WHERE id = %s", (refs["conversation"],))
        assert cur.fetchone() == {"agent_view_id": None}


def test_a_user_with_conversations_cannot_be_deleted(scope):
    """E2 deactivates a user; it never deletes one out from under their history."""
    conn, refs = scope
    with conn.cursor() as cur, pytest.raises(pymysql.err.IntegrityError):
        cur.execute("DELETE FROM `user` WHERE id = %s", (refs["user"],))


def test_two_attempts_may_share_one_number(scope):
    """The pool-wait path refunds an attempt, so (job_id, attempt) is NOT unique."""
    conn, _ = scope
    with conn.cursor() as cur:
        for execution_id in ("exec-a", "exec-b"):
            cur.execute(
                "INSERT INTO execution (execution_id, job_id, attempt, status) "
                "VALUES (%s, 4242, 1, 'running')",
                (execution_id,),
            )
        cur.execute("DELETE FROM execution WHERE job_id = 4242")


def test_one_execution_id_is_claimed_once(scope):
    conn, _ = scope
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execution (execution_id, job_id, attempt, status) "
            "VALUES ('exec-dup', 1, 1, 'running')"
        )
        with pytest.raises(pymysql.err.IntegrityError):
            cur.execute(
                "INSERT INTO execution (execution_id, job_id, attempt, status) "
                "VALUES ('exec-dup', 2, 1, 'running')"
            )
        cur.execute("DELETE FROM execution WHERE execution_id = 'exec-dup'")


def test_one_source_row_yields_one_event(scope):
    """UNIQUE (source_kind, source_id) is what makes the outbox relay idempotent."""
    conn, refs = scope
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_event (conversation_id, kind, payload, source_kind, source_id) "
            "VALUES (%s, 'message.created', '{}', 'message', 7)",
            (refs["conversation"],),
        )
        with pytest.raises(pymysql.err.IntegrityError):
            cur.execute(
                "INSERT INTO conversation_event (conversation_id, kind, payload, source_kind, "
                "source_id) VALUES (%s, 'message.created', '{}', 'message', 7)",
                (refs["conversation"],),
            )
