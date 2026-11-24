"""The test policy at `CONCURRENT_WORKERS_STRESS_TEST` concurrent claims (SCL-1).

One label snapshot per claim, shared by the policy resolution and the tool risk assessment,
no write to the catalog tables, every capability gets the same frozen `stub` list, and exactly
one `execution.tool_risk` UPDATE per claim (the direct MCP servers, after the workspace).
"""
from __future__ import annotations

import json
import logging
import threading

import pytest

from agento.framework import consumer as consumer_module
from agento.framework.consumer import Consumer
from agento.framework.execution_hooks import RunProfile, mint_execution_id
from agento.framework.job_models import AgentType
from agento.framework.publisher import insert_job
from agento.framework.test_run import MAX_LABELS, TEST_MODE_ENV, load_label_snapshot
from agento.framework.tool_catalog import AccessError, create_label
from agento.modules.conversation.src.hooks import ConversationExecutionIds

from .conftest import CONCURRENT_WORKERS_STRESS_TEST, DB_STRESS, _test_connection, bootstrap_for_tests

pytestmark = DB_STRESS
N = CONCURRENT_WORKERS_STRESS_TEST
DEADLINE_S = 120.0
CATALOG = ("tool_catalog", "tool_label", "tool_label_assignment")


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    yield c
    with c.cursor() as cur:
        cur.execute("DELETE a FROM tool_label_assignment a JOIN tool_catalog c ON c.id = a.tool_id "
                    "JOIN tool_label l ON l.id = a.label_id WHERE c.source = 'module:jira' "
                    "AND c.tool_name = 'jira_search' AND l.label_key = 'stub-in-test'")
        cur.execute("DELETE FROM tool_catalog WHERE source = 'module:jira' AND alias = 't3-scale'")
        cur.execute("DELETE FROM tool_label WHERE name LIKE 't3 %%'")
    c.close()


@pytest.fixture
def view(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('tp_ws', 't')")
        cur.execute("SELECT id FROM workspace WHERE code = 'tp_ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'tp_av', 't')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'tp_av'")
        return cur.fetchone()["id"]


def _checksum(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(f"CHECKSUM TABLE {', '.join(CATALOG)}")
        return [(r["Table"], r["Checksum"]) for r in cur.fetchall()]


def _override_jira_search(conn) -> None:
    """An admin override the claims must all see: `stub-in-test` added to jira_search."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT IGNORE INTO tool_catalog (source, tool_name, alias, default_labels, contract_hash) "
            "VALUES ('module:jira', 'jira_search', 't3-scale', '[]', %s)", ("0" * 40,))
        cur.execute("SELECT id FROM tool_catalog WHERE source = 'module:jira' AND tool_name = 'jira_search'")
        tool = cur.fetchone()["id"]
        cur.execute("SELECT id FROM tool_label WHERE label_key = 'stub-in-test'")
        label = cur.fetchone()["id"]
        cur.execute("INSERT INTO tool_label_assignment (tool_id, label_id, mode) VALUES (%s, %s, 'add') "
                    "ON DUPLICATE KEY UPDATE mode = 'add'", (tool, label))


def test_claim_tool_policy_scale(conn, view, int_db_config, int_consumer_config, monkeypatch):
    monkeypatch.setenv(TEST_MODE_ENV, "1")
    bootstrap_for_tests()
    _override_jira_search(conn)
    with conn.cursor() as cur:
        for i in range(N):
            job_id = insert_job(cur, agent_type=AgentType.TODO, source="jira", agent_view_id=view,
                                reference_id=f"TP-{i}", idempotency_key=f"tp:{i}", priority=50,
                                max_attempts=1, requester=None)
            cur.execute("UPDATE job SET test_config = %s WHERE id = %s", ('{"stub": [], "real": []}', job_id))
    before = _checksum(conn)

    calls: list[int] = []
    loaded: dict[int, object] = {}
    assessed: dict[int, object] = {}
    recorded: list[int] = []
    lock = threading.Lock()

    def counted(c, ids):
        snapshot = load_label_snapshot(c, ids)
        with lock:
            calls.append(threading.get_ident())
            loaded[threading.get_ident()] = snapshot
        return snapshot

    real_assess = consumer_module.assess_run

    def assess_counted(c, view_id, policy, snapshot, manifests, **kw):
        with lock:
            assessed[threading.get_ident()] = snapshot
        return real_assess(c, view_id, policy, snapshot, manifests, **kw)

    real_record = ConversationExecutionIds.record_tool_risk

    def record_counted(self, **kw):
        with lock:
            recorded.append(threading.get_ident())
        return real_record(self, **kw)

    monkeypatch.setattr(consumer_module, "load_label_snapshot", counted)
    monkeypatch.setattr(consumer_module, "assess_run", assess_counted)
    monkeypatch.setattr(ConversationExecutionIds, "record_tool_risk", record_counted)
    errors: list[BaseException] = []
    barrier = threading.Barrier(N)

    def claim() -> None:
        """The claim sequence of `Consumer._run_job`, without the harness run."""
        consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("scale"))
        barrier.wait()
        try:
            job = consumer._try_dequeue()
            assert job is not None
            with consumer._db() as c:
                tool_risk = consumer._resolve_tool_policy(c, job)
                job.execution_id = mint_execution_id(
                    conn=c, job_id=job.id, attempt=job.attempt,
                    profile=RunProfile(harness="claude", provider="anthropic", model=None,
                                       credential_id=None, tool_risk=tool_risk))
                assert consumer._issue_run_capabilities(c, job) is not None
            assert job.execution_id is not None
            consumer._record_direct_servers(job, "claude", None, "http://toolbox:3001", tool_risk)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=claim) for _ in range(N)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=DEADLINE_S)

    assert not errors, errors[:3]
    assert len(calls) == N and len(set(calls)) == N
    assert all(assessed[t] is loaded[t] for t in loaded) and len(assessed) == N
    assert len(recorded) == N and len(set(recorded)) == N
    assert _checksum(conn) == before
    with conn.cursor() as cur:
        cur.execute("SELECT test_config FROM toolbox_capability WHERE job_id IS NOT NULL")
        stubs = {json.dumps(json.loads(r["test_config"])[2]["stub"]) for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS n FROM toolbox_capability WHERE job_id IS NOT NULL")
        assert cur.fetchone()["n"] == N
    assert len(stubs) == 1
    assert "jira_search" in json.loads(stubs.pop())
    with conn.cursor() as cur:
        cur.execute("SELECT e.tool_risk FROM execution e JOIN job j ON j.id = e.job_id "
                    "WHERE j.agent_view_id = %s", (view,))
        risks = [json.loads(r["tool_risk"]) for r in cur.fetchall()]
    assert len(risks) == N
    assert {(r["direct"], r["tool_coverage"]) for r in risks} == {("recorded", "complete")}


def test_label_count_capped(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM tool_label")
        have = cur.fetchone()["n"]
    for i in range(MAX_LABELS - have):
        create_label(conn, f"t3 cap {i}")
    with pytest.raises(AccessError):
        create_label(conn, "t3 cap over")
    assert len(load_label_snapshot(conn, []).labels) == MAX_LABELS
