"""The test policy at claim time, against a real database (Task 3, test-policy v2)."""
from __future__ import annotations

import json
import logging

import pytest

from agento.framework.consumer import Consumer
from agento.framework.job_store import set_job_runtime
from agento.framework.test_run import (
    CAPABILITY_ENVELOPE_TAG,
    TEST_MODE_ENV,
    load_label_snapshot,
    parse_test_config,
)
from agento.framework.tool_catalog import BUILTIN_LABEL_KEYS

from .conftest import _test_connection, bootstrap_for_tests, fetch_job, update_job

SOURCE = "module:t3_snap"


@pytest.fixture
def conn():
    c = _test_connection(autocommit=True)
    yield c
    with c.cursor() as cur:
        cur.execute("DELETE FROM tool_catalog WHERE source LIKE 'module:t3_%%'")
        cur.execute("DELETE FROM tool_label WHERE label_key LIKE 't3-%%'")
    c.close()


def _tool(cur, name: str, *, source: str = SOURCE, present: int = 1) -> int:
    cur.execute(
        "INSERT INTO tool_catalog (source, tool_name, alias, default_labels, contract_hash, present) "
        "VALUES (%s, %s, %s, '[]', %s, %s)",
        (source, name, name, "0" * 40, present),
    )
    return cur.lastrowid


def _label(cur, key: str) -> int:
    cur.execute("INSERT INTO tool_label (label_key, name, name_norm) VALUES (%s, %s, %s)", (key, key, key))
    return cur.lastrowid


def _label_id(cur, key: str) -> int:
    cur.execute("SELECT id FROM tool_label WHERE label_key = %s", (key,))
    return cur.fetchone()["id"]


def test_snapshot_keeps_all_definitions_and_absent_tool_overrides(conn):
    with conn.cursor() as cur:
        _label(cur, "t3-empty")
        absent = _tool(cur, "back_again", present=0)
        cur.execute("INSERT INTO tool_label_assignment (tool_id, label_id, mode) VALUES (%s, %s, 'add'), "
                    "(%s, %s, 'remove')",
                    (absent, _label_id(cur, "external-communication"), absent, _label_id(cur, "stub-in-test")))

    snapshot = load_label_snapshot(conn, [(SOURCE, "back_again")])

    assert {*BUILTIN_LABEL_KEYS, "t3-empty"} <= set(snapshot.labels)
    assert snapshot.adds["external-communication"] == {"t3_snap/back_again"}
    assert snapshot.removes["stub-in-test"] == {"t3_snap/back_again"}


def test_snapshot_ignores_retired_history(conn):
    with conn.cursor() as cur:
        labels = [_label_id(cur, k) for k in BUILTIN_LABEL_KEYS] + [_label(cur, "t3-extra")]
        cur.executemany(
            "INSERT INTO tool_catalog (source, tool_name, alias, default_labels, contract_hash, present) "
            "VALUES (%s, %s, %s, '[]', %s, 0)",
            [("module:t3_retired", f"old_{i}", f"old_{i}", "0" * 40) for i in range(5000)],
        )
        cur.execute("INSERT INTO tool_label_assignment (tool_id, label_id, mode) "
                    "SELECT c.id, l.id, 'add' FROM tool_catalog c JOIN tool_label l "
                    f"ON l.id IN ({', '.join(['%s'] * len(labels))}) "
                    "WHERE c.source = 'module:t3_retired'", labels)
        assert cur.rowcount == 25000
        active = _tool(cur, "live")
        cur.execute("INSERT INTO tool_label_assignment (tool_id, label_id, mode) VALUES (%s, %s, 'add')",
                    (active, labels[-1]))

    snapshot = load_label_snapshot(conn, [(SOURCE, "live")])

    assert snapshot.adds == {"t3-extra": {"t3_snap/live"}}


def _job(cur, view: int, key: str, test_config: str | None = None) -> int:
    cur.execute(
        "INSERT INTO job (type, source, reference_id, idempotency_key, status, agent_view_id, test_config) "
        "VALUES ('blank', 'jira', %s, %s, 'TODO', %s, %s)",
        (key, key, view, test_config),
    )
    return cur.lastrowid


def _consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


def test_malformed_head_job_does_not_block_queue(
    conn, int_db_config, int_consumer_config, mock_claude, int_agent_view,
):
    bootstrap_for_tests()
    with conn.cursor() as cur:
        bad = _job(cur, int_agent_view, "T3-BAD", '{"v": 3}')
        good = _job(cur, int_agent_view, "T3-GOOD")
    consumer = _consumer(int_db_config, int_consumer_config)

    first = consumer._try_dequeue()
    assert first is not None and first.id == bad
    consumer._execute_job(first)
    second = consumer._try_dequeue()
    assert second is not None and second.id == good
    consumer._execute_job(second)

    bad_row = fetch_job(bad)
    assert (bad_row["status"], bad_row["error_class"]) == ("DEAD", "InvalidTestConfig")
    assert bad_row["test_config"] is not None and json.loads(bad_row["test_config"]) == {"v": 3}
    assert fetch_job(good)["status"] == "SUCCESS"
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM toolbox_capability WHERE job_id = %s", (bad,))
        assert cur.fetchone()["n"] == 0


def test_stale_recovery_with_malformed_policy(conn, int_db_config, int_consumer_config, int_agent_view):
    with conn.cursor() as cur:
        job_id = _job(cur, int_agent_view, "T3-STALE", "[1]")
    update_job(job_id, status="RUNNING", attempt=1, pid=99999)

    _consumer(int_db_config, int_consumer_config)._recover_stale_jobs()

    assert fetch_job(job_id)["status"] == "TODO"


def test_set_runtime_then_claim_stores_v2_on_job_and_envelope_on_capability(
    conn, int_db_config, int_consumer_config, mock_claude, int_agent_view, monkeypatch,
):
    monkeypatch.setenv(TEST_MODE_ENV, "1")
    bootstrap_for_tests()
    with conn.cursor() as cur:
        job_id = _job(cur, int_agent_view, "T3-RT")
    set_job_runtime(conn, job_id, test_config=parse_test_config('{"real": ["jira_add_comment"]}'))
    assert json.loads(fetch_job(job_id)["test_config"]) == {"stub": [], "real": ["jira_add_comment"]}

    consumer = _consumer(int_db_config, int_consumer_config)
    job = consumer._try_dequeue()
    assert job is not None and job.id == job_id
    consumer._execute_job(job)

    stored = json.loads(fetch_job(job_id)["test_config"])
    assert stored["v"] == 2 and stored["real"] == ["jira_add_comment"]
    assert "jira/jira_add_comment" in stored["known"]
    with conn.cursor() as cur:
        cur.execute("SELECT test_config FROM toolbox_capability WHERE job_id = %s", (job_id,))
        envelopes = [json.loads(r["test_config"]) for r in cur.fetchall()]
    assert envelopes and all(e == [CAPABILITY_ENVELOPE_TAG, 2, stored] for e in envelopes)


def test_label_usage_and_catalog_read_on_mysql(conn):
    from agento.framework.tool_catalog import list_labels, load_catalog

    with conn.cursor() as cur:
        count = _label(cur, "t3-count")
        _label(cur, "t3-unused")
        cur.execute(
            "INSERT INTO tool_catalog (source, tool_name, alias, default_labels, contract_hash, present) VALUES "
            "(%s, 'by_default', 'by_default', '[\"t3-count\"]', %s, 1), "
            "(%s, 'removed', 'removed', '[\"t3-count\"]', %s, 1), "
            "(%s, 'gone', 'gone', '[\"t3-count\"]', %s, 0)",
            (SOURCE, "0" * 40, SOURCE, "0" * 40, SOURCE, "0" * 40),
        )
        added = _tool(cur, "added")
        cur.execute("SELECT id FROM tool_catalog WHERE source = %s AND tool_name = 'removed'", (SOURCE,))
        cur.execute("INSERT INTO tool_label_assignment (tool_id, label_id, mode) VALUES (%s, %s, 'add'), "
                    "(%s, %s, 'remove')", (added, count, cur.fetchone()["id"], count))

    usage = {label["key"]: label["usage"] for label in list_labels(conn)}
    tools = {t.tool_name: t.labels for t in load_catalog(conn, manifests=()) if t.source == SOURCE}

    assert (usage["t3-count"], usage["t3-unused"]) == (2, 0)
    assert tools == {"by_default": {"t3-count"}, "removed": frozenset(), "added": {"t3-count"}}
