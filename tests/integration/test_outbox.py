"""The framework outbox (PRD E3-E5 §6.4.1).

A transition and the row that announces it are one transaction or they are a lie: the
writer takes the CALLER's cursor for exactly that reason.
"""
from __future__ import annotations

import json

import pytest

from agento.framework.outbox import PayloadError, prune_outbox, write_outbox

from .conftest import _test_connection


@pytest.fixture
def conn():
    c = _test_connection(autocommit=False)
    yield c
    c.close()


def _rows(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job_event_outbox ORDER BY id")
        return list(cur.fetchall())


def test_a_rolled_back_transition_leaves_no_row(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, reference_id, idempotency_key, status) "
                    "VALUES ('blank', 'test', 'OB-1', 'ob:1', 'TODO')")
        write_outbox(cur, job_id=cur.lastrowid, kind="job.claimed", payload={"attempt": 1})
    conn.rollback()

    assert _rows(conn) == []
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM job WHERE idempotency_key = 'ob:1'")
        assert cur.fetchone()["n"] == 0


def test_a_committed_transition_always_leaves_a_row(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, reference_id, idempotency_key, status) "
                    "VALUES ('blank', 'test', 'OB-2', 'ob:2', 'TODO')")
        job_id = cur.lastrowid
        row_id = write_outbox(cur, job_id=job_id, kind="job.claimed",
                              payload={"attempt": 1}, execution_id="exec-1")
    conn.commit()

    rows = _rows(conn)
    assert [r["id"] for r in rows] == [row_id]
    assert rows[0]["job_id"] == job_id
    assert rows[0]["kind"] == "job.claimed"
    assert rows[0]["execution_id"] == "exec-1"
    assert json.loads(rows[0]["payload"]) == {"attempt": 1}
    assert rows[0]["relayed_at"] is None


@pytest.mark.parametrize("payload", [
    # A payload is data that will be read back as JSON, never a live object.
    pytest.param({"conn": object()}, id="not-plain-data-object"),
    pytest.param({"nested": {"deeper": {"still": object()}}}, id="not-plain-data-nested-object"),
    pytest.param({"when": ...}, id="not-plain-data-ellipsis"),
    pytest.param(["not", "an", "object"], id="not-plain-data-list"),
    pytest.param("a string", id="not-plain-data-string"),
    pytest.param(None, id="not-plain-data-none"),
    # The outbox is read by a relay that fans out to clients; a config path or a token in
    # it is a secret one manifest away from a browser.
    pytest.param({"path": "core/smtp_pass"}, id="config-shaped-path"),
    pytest.param({"nested": {"CONFIG__CORE__SMTP_PASS": "x"}}, id="config-shaped-env-name"),
    pytest.param({"value": "sk-ant-api03-deadbeef"}, id="config-shaped-token"),
])
def test_a_payload_that_is_not_plain_safe_data_is_refused(conn, payload):
    with conn.cursor() as cur, pytest.raises(PayloadError):
        write_outbox(cur, job_id=1, kind="job.claimed", payload=payload)
    conn.rollback()


def test_a_plain_payload_is_accepted(conn):
    with conn.cursor() as cur:
        write_outbox(cur, job_id=1, kind="job.claimed",
                     payload={"attempt": 2, "ok": True, "note": None, "tags": ["a", "b"]})
    conn.commit()
    assert len(_rows(conn)) == 1


@pytest.mark.parametrize("kind", ["", "x" * 33, "Job.Claimed", "job claimed", 5])
def test_an_invalid_kind_is_refused(conn, kind):
    with conn.cursor() as cur, pytest.raises(PayloadError):
        write_outbox(cur, job_id=1, kind=kind, payload={})
    conn.rollback()


# --- retention: the framework prunes its own rows, relayed or not -------------------


def _aged(conn, days: int, *, relayed: bool) -> int:
    with conn.cursor() as cur:
        row_id = write_outbox(cur, job_id=1, kind="job.claimed", payload={"d": days})
        cur.execute(
            "UPDATE job_event_outbox SET created_at = NOW() - INTERVAL %s DAY, "
            "relayed_at = IF(%s, NOW() - INTERVAL %s DAY, NULL) WHERE id = %s",
            (days, relayed, days, row_id),
        )
    conn.commit()
    return row_id


def test_the_prune_removes_old_rows_that_were_never_relayed(conn):
    """With the module disabled NOTHING is ever relayed, so a relayed-only prune would
    keep every framework row for ever (CODE-8)."""
    old = _aged(conn, 40, relayed=False)
    fresh = _aged(conn, 1, relayed=False)

    assert prune_outbox(conn, retention_days=30) == 1
    assert [r["id"] for r in _rows(conn)] == [fresh]
    assert old not in [r["id"] for r in _rows(conn)]


def test_the_prune_removes_old_relayed_rows_too(conn):
    _aged(conn, 40, relayed=True)
    assert prune_outbox(conn, retention_days=30) == 1


def test_a_row_inside_the_window_survives_to_be_relayed(conn):
    kept = _aged(conn, 29, relayed=False)
    assert prune_outbox(conn, retention_days=30) == 0
    assert [r["id"] for r in _rows(conn)] == [kept]


def test_the_comparison_is_strictly_older_than(conn):
    """An hour inside the window is inside it; an hour past it is not."""
    with conn.cursor() as cur:
        inside = write_outbox(cur, job_id=1, kind="job.claimed", payload={})
        cur.execute("UPDATE job_event_outbox SET created_at = NOW() - INTERVAL 30 DAY "
                    "+ INTERVAL 1 HOUR WHERE id = %s", (inside,))
        outside = write_outbox(cur, job_id=1, kind="job.claimed", payload={})
        cur.execute("UPDATE job_event_outbox SET created_at = NOW() - INTERVAL 30 DAY "
                    "- INTERVAL 1 HOUR WHERE id = %s", (outside,))
    conn.commit()

    assert prune_outbox(conn, retention_days=30) == 1
    assert [r["id"] for r in _rows(conn)] == [inside]


def test_the_retention_comes_from_config_when_not_given(conn):
    _aged(conn, 40, relayed=False)
    assert prune_outbox(conn) == 1          # core/outbox/retention_days defaults to 30
