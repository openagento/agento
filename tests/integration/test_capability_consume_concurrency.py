"""A single-use capability is consumed atomically: of concurrent presentations, exactly one wins.

Runs the toolbox dispatcher's OWN statement (read from dispatcher.js, not restated) from
several connections at once against a real MySQL.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import agento.framework as fw
from agento.framework.toolbox_capability import issue_capability

from .conftest import _test_connection

_DISPATCHER = Path(fw.__file__).parents[1] / "toolbox" / "dispatcher.js"
N = 8


def _consume_sql() -> str:
    src = _DISPATCHER.read_text()
    block = re.search(r"export const CONSUME_CAPABILITY_SQL =(.*?);\n", src, re.S).group(1)
    return "".join(re.findall(r"'([^']*)'", block)).replace("?", "%s")


def _agent_view_id(cur) -> int:
    # Not the `int_agent_view` fixture: its jira ingress binding outlives the test and would
    # reroute every publish test that sorts after this file.
    cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('dev', 'dev')")
    cur.execute("SELECT id FROM workspace WHERE code = 'dev'")
    workspace_id = cur.fetchone()["id"]
    cur.execute(
        "INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'developer', 'developer')",
        (workspace_id,),
    )
    cur.execute("SELECT id FROM agent_view WHERE code = 'developer'")
    return cur.fetchone()["id"]


def test_concurrent_consumption_has_exactly_one_winner():
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            view_id = _agent_view_id(cur)
        issue_capability(
            conn, kind="mcp_job", agent_view_id=view_id, job_id=1, ttl_seconds=60,
            allowed_transports=["http"],
        )
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM toolbox_capability")
            capability_id = cur.fetchone()["id"]
    finally:
        conn.close()

    sql = _consume_sql()
    assert "consumed_at IS NULL" in sql
    barrier = threading.Barrier(N)
    counts: list[int] = []
    lock = threading.Lock()

    def consume():
        c = _test_connection(autocommit=True)
        try:
            barrier.wait()
            with c.cursor() as cur:
                affected = cur.execute(sql, (capability_id,))
            with lock:
                counts.append(affected)
        finally:
            c.close()

    threads = [threading.Thread(target=consume) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(counts) == [0] * (N - 1) + [1]
