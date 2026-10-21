"""Nested sub-agents against the real schema: the real `agent_run` / `agent_result` /
`schedule_followup` handlers (tests/integration/node/subagent_tool.mjs, real `enqueueJob`) and the
real `Consumer` that runs the child job with a patched harness runner.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.consumer import Consumer
from agento.framework.db_grants import TOOLBOX_GRANTS
from agento.modules.claude.src.output_parser import ClaudeResult

from .conftest import (
    CONCURRENT_WORKERS_STRESS_TEST,
    DB_STRESS,
    TEST_DB,
    _root_connection,
    _test_connection,
    bootstrap_for_tests,
    fetch_all_jobs,
    fetch_job,
    insert_primary_token,
    insert_queued_job,
    update_job,
)

_ROOT = Path(__file__).resolve().parents[2]
_DRIVER = Path(__file__).parent / "node" / "subagent_tool.mjs"
CONFIGS = {"subagent": {"allowed_agent_views": "", "max_children_per_job": "10"}}

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (_ROOT / "src/agento/toolbox/node_modules/mysql2").is_dir(),
    reason="needs node and the toolbox's npm install")


_DRIVER_ENV: dict[str, str] = {}


@pytest.fixture(scope="module", autouse=True)
def toolbox_db_user():
    """The driver connects as a user with exactly the toolbox's production grants
    (db_grants.TOOLBOX_GRANTS) on this worker's database, so SQL the toolbox user may not run
    fails here as it does in a deployment. Not apply_grants: on this shared server it would
    reset the live `agento_toolbox` user."""
    user, password = f"agento_toolbox_it_{TEST_DB[-12:]}", secrets.token_hex(16)
    conn = _root_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (TEST_DB,))
            tables = {row[0] for row in cur.fetchall()}
            cur.execute("DROP USER IF EXISTS %s@'%%'", (user,))
            cur.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (user, password))
            for table, privileges in TOOLBOX_GRANTS.items():
                if table in tables:
                    cur.execute(f"GRANT {privileges} ON `{TEST_DB}`.`{table}` TO %s@'%%'", (user,))
        _DRIVER_ENV.update(TEST_MYSQL_USER=user, TEST_MYSQL_PASSWORD=password)
        yield
        _DRIVER_ENV.clear()
        with conn.cursor() as cur:
            cur.execute("DROP USER IF EXISTS %s@'%%'", (user,))
    finally:
        conn.close()


def _call(tool, args, job_id, view, configs=CONFIGS):
    return {"tool": tool, "args": args, "jobId": job_id, "agentViewId": view, "moduleConfigs": configs}


def _run(*calls):
    out = subprocess.run(["node", str(_DRIVER)], input=json.dumps({"calls": list(calls)}),
                         capture_output=True, text=True, check=True, timeout=120,
                         env={**os.environ, **_DRIVER_ENV})
    return json.loads(out.stdout)


def _spawn(job_id, view, prompt="Summarize the logs."):
    [res] = _run(_call("agent_run", {"agent_view": "developer", "prompt": prompt}, job_id, view))
    assert not res["isError"], res["text"]
    return json.loads(res["text"])


def _result(job_id, child, view):
    [res] = _run(_call("agent_result", {"job_id": child}, job_id, view))
    return res


def _root(view, key="root:1"):
    job_id = insert_queued_job(job_type="blank", idempotency_key=key, agent_view_id=view)
    update_job(job_id, status="RUNNING")
    return job_id


def _count(sql, params=()):
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()["n"]
    finally:
        conn.close()


def test_agent_run_child_runs_in_the_consumer_and_agent_result_returns_its_output(
    int_agent_view, int_db_config, int_consumer_config
):
    root = _root(int_agent_view)
    spawned = _spawn(root, int_agent_view, "Count the open tickets.")
    assert spawned["status"] == "TODO" and spawned["depth"] == 1
    child = fetch_job(spawned["job_id"])
    assert (child["type"], child["parent_job_id"], child["depth"], child["agent_view_id"]) == (
        "subagent", root, 1, int_agent_view)
    assert _count("SELECT COUNT(*) AS n FROM job_event_outbox WHERE job_id = %s AND kind = 'job.queued'",
                  (child["id"],)) == 1
    assert json.loads(_result(root, child["id"], int_agent_view)["text"]) == {
        "job_id": child["id"], "status": "TODO"}

    insert_primary_token("claude")
    captured = {}

    def run(request):
        captured["prompt"] = request.prompt
        return ClaudeResult(raw_output="42 open tickets", input_tokens=1, output_tokens=1, cost_usd=0.0,
                            num_turns=1, duration_ms=1, session_id="s")

    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
    with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", side_effect=run):
        job = consumer._try_dequeue()
        assert job is not None and job.id == child["id"]
        consumer._execute_job(job)

    assert captured["prompt"].startswith(f"Task delegated by Agento job #{root}.")
    assert captured["prompt"].endswith("Count the open tickets.")
    assert fetch_job(child["id"])["status"] == "SUCCESS"
    assert json.loads(_result(root, child["id"], int_agent_view)["text"]) == {
        "job_id": child["id"], "status": "SUCCESS", "output": "42 open tickets"}


def test_three_levels_then_the_fourth_is_refused_with_no_new_row(int_agent_view):
    job_id = _root(int_agent_view)
    for depth in (1, 2, 3):
        spawned = _spawn(job_id, int_agent_view)
        assert spawned["depth"] == depth
        row = fetch_job(spawned["job_id"])
        assert (row["parent_job_id"], row["depth"]) == (job_id, depth)
        job_id = spawned["job_id"]

    before = len(fetch_all_jobs())
    [res] = _run(_call("agent_run", {"agent_view": "developer", "prompt": "deeper"}, job_id, int_agent_view))
    assert res == {"text": "sorry, max 3 nested subagents levels", "isError": True}
    assert len(fetch_all_jobs()) == before


def test_only_the_direct_parent_reads_a_child(int_agent_view):
    root = _root(int_agent_view)
    a = _spawn(root, int_agent_view)["job_id"]
    b = _spawn(root, int_agent_view)["job_id"]
    grandchild = _spawn(a, int_agent_view)["job_id"]

    denied = {"text": "job not found or not accessible", "isError": True}
    assert _result(a, b, int_agent_view) == denied            # sibling
    assert _result(root, grandchild, int_agent_view) == denied  # grandparent
    assert _result(grandchild, a, int_agent_view) == denied     # child reading its parent
    assert _result(root, 999999, int_agent_view) == denied      # nonexistent
    assert not _result(a, grandchild, int_agent_view)["isError"]


def test_a_delegated_job_cannot_schedule_a_followup(int_agent_view):
    child = _spawn(_root(int_agent_view), int_agent_view)["job_id"]
    before = len(fetch_all_jobs())
    at = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    [res] = _run(_call("schedule_followup", {"user": "agent@example.com", "scheduled_at": at,
                                             "instructions": "Check the result again later."},
                       child, int_agent_view))
    assert res == {"text": "schedule_followup is not available in a delegated job", "isError": True}
    assert len(fetch_all_jobs()) == before


@DB_STRESS
def test_concurrent_calls_of_one_parent_hold_the_child_cap(int_agent_view):
    root = _root(int_agent_view)
    results = _run(*[_call("agent_run", {"agent_view": "developer", "prompt": f"task {i}"}, root, int_agent_view)
                     for i in range(CONCURRENT_WORKERS_STRESS_TEST)])

    assert sum(not r["isError"] for r in results) == 10
    refusals = [r["text"] for r in results if r["isError"]]
    assert refusals == ["max 10 child jobs per job reached"] * (CONCURRENT_WORKERS_STRESS_TEST - 10)
    assert _count("SELECT COUNT(*) AS n FROM job WHERE parent_job_id = %s", (root,)) == 10
    assert _count("SELECT COUNT(*) AS n FROM job_event_outbox o JOIN job j ON j.id = o.job_id "
                  "WHERE j.parent_job_id = %s AND o.kind = 'job.queued'", (root,)) == 10


@DB_STRESS
def test_independent_parents_each_get_their_child(int_agent_view):
    roots = [_root(int_agent_view, key=f"root:{i}") for i in range(CONCURRENT_WORKERS_STRESS_TEST)]
    results = _run(*[_call("agent_run", {"agent_view": "developer", "prompt": "task"}, r, int_agent_view)
                     for r in roots])

    assert [r["isError"] for r in results] == [False] * len(roots)
    assert _count("SELECT COUNT(DISTINCT parent_job_id) AS n FROM job WHERE depth = 1") == len(roots)


@pytest.fixture
def full_bootstrap_after():
    """The test bootstraps without `subagent`; every later test needs it back. Requested
    before `monkeypatch`, so the module filter is already undone when this tears down."""
    yield
    bootstrap_for_tests()


def test_waiting_subagent_rows_do_not_stall_the_queue_while_the_module_is_disabled(
    full_bootstrap_after, int_db_config, int_consumer_config, monkeypatch
):
    import agento.framework.bootstrap as bootstrap_module

    monkeypatch.setattr(bootstrap_module, "filter_enabled",
                        lambda manifests: [m for m in manifests if m.name != "subagent"])
    bootstrap_for_tests()

    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO job (type, source, priority, reference_id, context, idempotency_key, status, "
                "attempt, max_attempts, created_at) VALUES ('subagent', 'subagent', 90, '1', 'p', %s, 'TODO', "
                "0, 3, NOW() - INTERVAL 1 HOUR)",
                [(f"subagent:1:{i}",) for i in range(300)])
    finally:
        conn.close()
    blank = insert_queued_job(job_type="blank", idempotency_key="blank:1", reference_id="BLANK-1")

    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
    job = consumer._try_dequeue()

    assert job is not None and job.id == blank
    assert _count("SELECT COUNT(*) AS n FROM job WHERE type = 'subagent' AND status = 'TODO' "
                  "AND attempt = 0 AND started_at IS NULL") == 300

    monkeypatch.undo()
    bootstrap_for_tests()
    job = consumer._try_dequeue()
    assert job is not None and fetch_job(job.id)["type"] == "subagent"
