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
from agento.framework.events import JobSucceededEvent
from agento.framework.job_models import Job
from agento.framework.job_types import resolve_job_type
from agento.framework.publish_service import publish_job
from agento.modules.claude.src.output_parser import ClaudeResult
from agento.modules.subagent.src.observers import WakeParentObserver, wake_key

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


def _spawn(caller, view, prompt="Summarize the logs.", *, target="developer", configs=CONFIGS, **args):
    [res] = _run(_call("agent_run", {"agent_view": target, "prompt": prompt, **args}, caller, view, configs))
    assert not res["isError"], res["text"]
    return json.loads(res["text"])


def _result(job_id, child, view):
    [res] = _run(_call("agent_result", {"job_id": child}, job_id, view))
    return res


def _root(view, key="root:1"):
    """A running root turn in its own thread (the reference names the thread)."""
    job_id = publish_job(source="jira", agent_type=resolve_job_type("blank"), agent_view_id=view,
                         reference_id=key, idempotency_key=key, requester=None, priority=50)
    update_job(job_id, status="RUNNING")
    return job_id


def _finish(job_id, session_id="s"):
    """End a run as the consumer does: commit SUCCESS, then dispatch job_succeed_after."""
    update_job(job_id, status="SUCCESS", session_id=session_id, finished_at=datetime.now(UTC))
    WakeParentObserver().execute(JobSucceededEvent(job=Job.from_row(fetch_job(job_id))))


def _wake_of(job_id):
    return next((j for j in fetch_all_jobs() if j["idempotency_key"] == wake_key(job_id)), None)


@pytest.fixture
def reviewer(int_agent_view):
    """A second view of the same workspace."""
    conn = _test_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) "
                        "SELECT workspace_id, 'reviewer', 'reviewer' FROM agent_view WHERE id = %s",
                        (int_agent_view,))
            cur.execute("SELECT id FROM agent_view WHERE code = 'reviewer'")
            return cur.fetchone()["id"]
    finally:
        conn.close()


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
    # The child runs in its parent's thread (and so its conversation), as a line of its own.
    assert (child["thread_id"], child["line_id"]) == (fetch_job(root)["thread_id"], None)
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


def test_only_the_callers_line_reads_a_child(int_agent_view):
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
    assert refusals == ["max 10 sub-agents for this agent"] * (CONCURRENT_WORKERS_STRESS_TEST - 10)
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
    blank = publish_job(source="jira", agent_type=resolve_job_type("blank"), agent_view_id=None,
                        reference_id="BLANK-1", idempotency_key="blank:1", requester=None, priority=50)

    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
    job = consumer._try_dequeue()

    assert job is not None and job.id == blank
    assert _count("SELECT COUNT(*) AS n FROM job WHERE type = 'subagent' AND status = 'TODO' "
                  "AND attempt = 0 AND started_at IS NULL") == 300

    monkeypatch.undo()
    bootstrap_for_tests()
    job = consumer._try_dequeue()
    assert job is not None and fetch_job(job.id)["type"] == "subagent"


# --- lines, the scope rule and the wake (plan §8) -------------------------------------

def test_a_wake_turn_resumes_the_parent_and_reads_and_continues_its_children(
    int_agent_view, int_db_config, int_consumer_config
):
    """The real consumer runs both children; the last one's end wakes the parent. The wake
    turn is a row of the parent's line: the claim resumes the parent's session, and the turn
    reaches the children the parent started."""
    root = _root(int_agent_view)
    kids = [_spawn(root, int_agent_view, f"task {i}")["job_id"] for i in range(2)]
    update_job(root, status="SUCCESS", session_id="s-root", finished_at=datetime.now(UTC))

    insert_primary_token("claude")
    consumer = Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))
    result = ClaudeResult(raw_output="done", input_tokens=1, output_tokens=1, cost_usd=0.0,
                          num_turns=1, duration_ms=1, session_id="s-kid")
    with patch("agento.modules.claude.src.runner.ClaudeSubprocessRunner.execute", return_value=result):
        for _ in kids:
            job = consumer._try_dequeue()
            assert job is not None and job.id in kids
            consumer._execute_job(job)

    wake = _wake_of(root)
    assert wake is not None and [j["id"] for j in fetch_all_jobs()
                                 if j["idempotency_key"].startswith("subagent_wake:")] == [wake["id"]]
    claimed = consumer._try_dequeue()
    assert claimed is not None and (claimed.id, claimed.resume_job_id) == (wake["id"], root)

    for kid in kids:
        assert json.loads(_result(wake["id"], kid, int_agent_view)["text"])["output"] == "done"
    again = _spawn(wake["id"], int_agent_view, "Fix the review findings.", job_id=kids[0])
    row = fetch_job(again["job_id"])
    assert (row["parent_job_id"], row["depth"], row["line_id"], row["thread_id"]) == (
        wake["id"], 1, kids[0], wake["thread_id"])


def test_five_rounds_of_one_child_stay_one_line(int_agent_view):
    root = _root(int_agent_view)
    head = _spawn(root, int_agent_view)["job_id"]
    latest = head
    for _ in range(5):
        _finish(latest)
        # Either the first row or the latest one names the line.
        latest = _spawn(root, int_agent_view, "next round", job_id=latest)["job_id"]
        assert fetch_job(latest)["line_id"] == head
    assert not _result(root, latest, int_agent_view)["isError"]


def test_a_continuation_waits_for_its_line_and_keeps_its_view(int_agent_view, reviewer):
    root = _root(int_agent_view)
    head = _spawn(root, int_agent_view)["job_id"]
    configs = {"subagent": {**CONFIGS["subagent"], "allowed_agent_views": "reviewer"}}

    def again(target):
        [res] = _run(_call("agent_run", {"agent_view": target, "prompt": "x", "job_id": head},
                           root, int_agent_view, configs))
        return res

    assert again("developer") == {"text": "job is still running", "isError": True}
    _finish(head)
    assert again("reviewer") == {"text": "job not found or not accessible", "isError": True}
    assert not again("developer")["isError"]


def test_a_sibling_branch_in_another_view_is_out_of_scope(int_agent_view, reviewer):
    configs = {"subagent": {**CONFIGS["subagent"], "allowed_agent_views": "developer,reviewer"}}
    root = _root(int_agent_view)
    a = _spawn(root, int_agent_view, configs=configs)["job_id"]
    b = _spawn(root, int_agent_view, target="reviewer", configs=configs)["job_id"]
    a1 = _spawn(a, int_agent_view, configs=configs)["job_id"]
    _finish(a1)

    denied = {"text": "job not found or not accessible", "isError": True}
    assert _result(b, a1, reviewer) == denied
    [res] = _run(_call("agent_run", {"agent_view": "developer", "prompt": "x", "job_id": a1},
                       b, reviewer, configs))
    assert res == denied
    assert not _result(a, a1, int_agent_view)["isError"]
    assert not _result(root, b, int_agent_view)["isError"]  # a child in another view


def test_the_cap_holds_per_line_and_bounds_the_rows_under_one_parent(int_agent_view):
    """With cap 2: the root starts 2 lines; each line runs 2 delegations, so 2 wakes; its
    third delegation is refused. The rows under the root are cap + cap * cap = 6."""
    cap = 2
    configs = {"subagent": {**CONFIGS["subagent"], "max_children_per_job": str(cap)}}
    root = _root(int_agent_view)
    lines = [_spawn(root, int_agent_view, configs=configs)["job_id"] for _ in range(cap)]
    [res] = _run(_call("agent_run", {"agent_view": "developer", "prompt": "x"}, root, int_agent_view, configs))
    assert res == {"text": f"max {cap} sub-agents for this agent", "isError": True}
    _finish(root)

    for line in lines:
        turn = line
        for _ in range(cap):
            update_job(turn, status="RUNNING")
            grandchild = _spawn(turn, int_agent_view, configs=configs)["job_id"]
            _finish(turn)
            _finish(grandchild)
            turn = _wake_of(turn)["id"]          # the line's next turn
            assert fetch_job(turn)["line_id"] == line
        update_job(turn, status="RUNNING")
        [res] = _run(_call("agent_run", {"agent_view": "developer", "prompt": "x"}, turn, int_agent_view,
                           configs))
        assert res == {"text": f"max {cap} sub-agents for this agent", "isError": True}
        _finish(turn)

    under_root = [j for j in fetch_all_jobs() if j["parent_job_id"] == root]
    assert len(under_root) == cap + cap * cap
    assert _wake_of(root) is not None
