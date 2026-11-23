"""The jobs grid search and sort on real MySQL: the panel's old client-side semantics, now in SQL."""
from __future__ import annotations

import pytest

from agento.framework.admin import data
from agento.web import grid

from .conftest import _test_connection

# (id, type, status, view code or None, reference, started→finished seconds or None)
JOBS = [
    (123, "sync", "SUCCESS", "dev", "AG-7", 30),
    (124, "todo", "FAILED", None, "abc", 5),
    (125, "todo", "SUCCESS", None, "abc", None),
    (126, "todo", "TODO", "dev", "café", None),
    (127, "todo", "DEAD", "dev", "50% off_now c:\\tmp", 600),
]


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    with c.cursor() as cur:
        cur.execute("SHOW FULL COLUMNS FROM job WHERE Field = 'reference_id'")
        column = cur.fetchone()
        # Accent- and case-sensitive data and session: only the query's own COLLATE can fold them.
        cur.execute("ALTER TABLE job MODIFY reference_id VARCHAR(512) NULL COLLATE utf8mb4_bin")
        cur.execute("SET collation_connection = utf8mb4_bin")
        cur.execute("INSERT INTO workspace (code, label) VALUES ('grid-ws', 'Grid WS')")
        cur.execute("INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, 'dev', 'Dev')",
                    (cur.lastrowid,))
        av = cur.lastrowid
        for job_id, type_, status, view, ref, seconds in JOBS:
            cur.execute(
                "INSERT INTO job (id, type, source, agent_view_id, reference_id, idempotency_key, status, "
                "prompt, started_at, finished_at) VALUES (%s, %s, 'jira', %s, %s, %s, %s, '', "
                "'2026-11-01 10:00:00', DATE_ADD('2026-11-01 10:00:00', INTERVAL %s SECOND))",
                (job_id, type_, av if view else None, ref, f"grid:{job_id}", status, seconds),
            )
    try:
        yield c
    finally:
        with c.cursor() as cur:
            cur.execute(f"ALTER TABLE job MODIFY reference_id {column['Type']} NULL COLLATE {column['Collation']}")
            cur.execute("DELETE FROM agent_view WHERE code = 'dev'")
            cur.execute("DELETE FROM workspace WHERE code = 'grid-ws'")
        c.close()


def _ids(conn, q, status=None) -> list[int]:
    search = grid.contains(q)
    rows, total = data.get_job_page(conn, limit=50, offset=0, search=search, status=status)
    assert total == len(rows) == data.count_jobs(conn, search=search, status=status)
    return sorted(r["id"] for r in rows)


@pytest.mark.parametrize("q, ids", [
    ("ag-7", [123]),                 # case
    ("cafe", [126]),                 # accent
    ("CAFÉ", [126]),
    ("23", [123]),                   # a substring of the id
    ("123 sync", [123]),             # across fields
    ("SUCCESS  abc", [125]),         # a NULL view is an empty part: two spaces
    ("SUCCESS abc", []),
    ("50%", [127]),                  # wildcards match literally
    ("off_now", [127]),
    ("of_", []),                   # "_" is not "any one character" ("off")
    ("c:\\tmp", [127]),
    ("%", [127]),
    ("_", [127]),
])
def test_search(conn, q, ids):
    assert _ids(conn, q) == ids


def test_search_with_status(conn):
    assert _ids(conn, "abc", status="FAILED") == [124]


def _order(conn, key, desc):
    return [r["id"] for r in data.get_job_page(conn, limit=50, offset=0, order=(key, desc))[0]]


def test_status_sorts_as_text_not_enum_order(conn):
    assert _order(conn, "status", False) == [127, 124, 123, 125, 126]


def test_null_order_is_mysqls_and_ties_break_on_id(conn):
    # Two NULL durations: first ascending, last descending; the id tie-break follows the direction.
    assert _order(conn, "duration", False) == [125, 126, 124, 123, 127]
    assert _order(conn, "duration", True) == [127, 123, 124, 126, 125]
    assert _order(conn, "view", False) == [124, 125, 123, 126, 127]
    assert _order(conn, "view", True) == [127, 126, 123, 125, 124]


@pytest.mark.parametrize("q", [None, "todo"])
def test_a_page_past_the_end_has_no_rows_and_the_real_total(conn, q):
    search = grid.contains(q) if q else None
    assert data.get_job_page(conn, limit=2, offset=10, search=search) == ([], 4 if q else len(JOBS))


def test_a_search_page_counts_every_match_not_the_page(conn):
    rows, total = data.get_job_page(conn, limit=2, offset=2, search=grid.contains("todo"))
    assert ([r["id"] for r in rows], total) == ([125, 124], 4)


def test_count_without_search(conn):
    assert data.count_jobs(conn) == len(JOBS)
    assert data.count_jobs(conn, status="SUCCESS") == 2


def test_get_jobs_keeps_its_filters_and_newest_first_for_the_tui_and_job_list(conn):
    rows = data.get_jobs(conn, source="jira", agent_view_code="dev", limit=2, offset=1, strict=True)
    assert [r["id"] for r in rows] == [126, 123]
    assert set(rows[0]) == {"id", "type", "status", "source", "reference_id", "agent_type", "created_at", "started_at",
                            "finished_at", "input_tokens", "output_tokens", "error_class", "error_message",
                            "agent_view_code"}


@pytest.mark.parametrize("key, indexed", [("id", True), ("ref", True), ("duration", False)])
def test_a_search_reads_the_search_index_when_it_covers_the_sort(conn, monkeypatch, key, indexed):
    seen = []
    real = type(conn.cursor()).execute
    monkeypatch.setattr(type(conn.cursor()), "execute", lambda self, q, a=None: seen.append((q, a)) or real(self, q, a))
    data.get_job_page(conn, limit=2, offset=0, search=grid.contains("todo"), order=(key, True))
    monkeypatch.undo()
    with conn.cursor() as cur:
        cur.execute("EXPLAIN " + seen[0][0], seen[0][1])
        keys = {r["key"] for r in cur.fetchall()}
    assert ("idx_job_search" in keys) is indexed
