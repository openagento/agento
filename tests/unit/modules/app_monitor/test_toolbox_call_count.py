"""``toolbox_mcp_calls`` = the calls the toolbox dispatcher audited on this attempt's
MCP capability (O5). No transcript, no harness, no execution id."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agento.framework.toolbox_capability import KIND_MCP_JOB
from agento.modules.app_monitor.src import observers as obs


class _Cursor:
    """DictCursor double: answers the capability lookup, then the audit count."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.conn.error:
            raise self.conn.error
        self.conn.queries.append((" ".join(sql.split()), params))

    def fetchone(self):
        sql, params = self.conn.queries[-1]
        if "FROM toolbox_capability" in sql:
            return {"id": self.conn.cap_id}
        return {"n": self.conn.audit_rows.get(params[0], 0)}


class _Conn:
    def __init__(self, cap_id=None, audit_rows=None, error=None):
        self.cap_id = cap_id
        self.audit_rows = audit_rows or {}
        self.error = error
        self.queries: list = []

    def cursor(self):
        return _Cursor(self)


def _job(agent_view_id=5, execution_id=None):
    return SimpleNamespace(id=7, agent_view_id=agent_view_id, execution_id=execution_id)


def test_counts_the_audit_rows_of_the_newest_mcp_capability():
    conn = _Conn(cap_id=42, audit_rows={42: 3, 41: 9})
    assert obs._count_toolbox_calls(conn, _job()) == 3
    (cap_sql, cap_params), (count_sql, count_params) = conn.queries
    assert "MAX(id)" in cap_sql and cap_params == (7, KIND_MCP_JOB)
    assert "FROM tool_invocation WHERE capability_id = %s" in count_sql
    assert count_params == (42,)
    # With the conversation module off ``execution_id`` is NULL; a NULL-safe filter on it
    # would match every attempt of the job.
    assert all("execution_id" not in sql for sql, _ in conn.queries)
    assert obs._count_toolbox_calls(_Conn(cap_id=42), _job()) == 0  # zero, not unknown


def test_no_agent_view_is_unknown_and_reads_nothing():
    conn = _Conn(cap_id=42)
    assert obs._count_toolbox_calls(conn, _job(agent_view_id=None)) is None
    assert conn.queries == []


def test_db_error_is_unknown(caplog):
    secret = "mysql://u:hunter2@db"
    conn = _Conn(error=RuntimeError(secret))
    assert obs._count_toolbox_calls(conn, _job()) is None
    assert secret not in caplog.text  # SEC-6: type name only


@pytest.mark.parametrize(("cap_id", "job"), [
    (None, _job()),  # no capability row
    (42, None),
    (42, SimpleNamespace(id=None, agent_view_id=5)),
])
def test_no_capability_or_job_id_is_unknown(cap_id, job):
    assert obs._count_toolbox_calls(_Conn(cap_id=cap_id), job) is None
