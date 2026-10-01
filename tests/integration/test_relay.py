"""The outbox relay (PRD E3-E5 §6.4.1).

The classification order is the contract: a row is classified by `job.source` first, never
by its reference value, and nothing is ever left pending.
"""
from __future__ import annotations

import json
import uuid

import pytest

from agento.framework.outbox import write_outbox
from agento.modules.conversation.src import relay, service

from .conftest import (
    _clean,  # noqa: F401
    )
from .test_conversation_submission import _job_type  # noqa: F401


def _events(conn, conversation_id=None) -> list[dict]:
    with conn.cursor() as cur:
        if conversation_id is None:
            cur.execute("SELECT * FROM conversation_event ORDER BY id")
        else:
            cur.execute("SELECT * FROM conversation_event WHERE conversation_id = %s "
                        "ORDER BY id", (conversation_id,))
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _outbox(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM job_event_outbox ORDER BY id")
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _relayed(conn) -> list[dict]:
    return [r for r in _outbox(conn) if r["relayed_at"] is not None]


def _write(conn, *, job_id, kind, payload=None, execution_id=None) -> int:
    with conn.cursor() as cur:
        row_id = write_outbox(cur, job_id=job_id, kind=kind, payload=payload or {},
                              execution_id=execution_id)
    conn.commit()
    return row_id


def _job(conn, *, source="jira", reference_id=None) -> int:
    """A bare job row. The relay only ever reads `source` and `reference_id` off it."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO job (source, type, agent_type, status, reference_id, prompt, "
            "idempotency_key) VALUES (%s, 'blank', 'claude', 'TODO', %s, 'p', %s)",
            (source, reference_id, f"test-relay:{uuid.uuid4()}"),
        )
        job_id = cur.lastrowid
    conn.commit()
    return job_id


@pytest.fixture
def thread(conn, world):
    """One conversation with one submitted turn, and its real job."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")
    # §4.3's publishing service already wrote this turn's `job.queued` row. Drain it, so
    # each test below measures only the rows it writes itself; the real one has its own
    # test (`test_a_real_submission_relays_its_job_queued_row`).
    with conn.cursor() as cur:
        cur.execute("DELETE FROM job_event_outbox")
        cur.execute("DELETE FROM conversation_event WHERE source_kind = 'outbox'")
    conn.commit()
    return {"conversation_id": cid, "message_id": message_id, "job_id": job_id}


def test_a_real_submission_relays_its_job_queued_row(conn, world):
    """No fixture drain here: §4.3 wrote the row in the job's own insert, and it is the
    first lifecycle event every thread has."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                           client_message_id="c1", content="pytanie")

    assert relay.relay_outbox(conn) == 1

    assert [e["kind"] for e in _events(conn, cid) if e["source_kind"] == "outbox"] \
        == ["job.queued"]


# --- the classification table (§6.4.1) ------------------------------------

def test_a_conversation_job_yields_one_event(conn, thread):
    _write(conn, job_id=thread["job_id"], kind="job.claimed", payload={"attempt": 1})

    assert relay.relay_outbox(conn) == 1

    events = [e for e in _events(conn, thread["conversation_id"])
              if e["source_kind"] == "outbox"]
    assert len(events) == 1
    assert events[0]["kind"] == "job.claimed"
    assert json.loads(events[0]["payload"]) == {"attempt": 1}
    assert not [r for r in _outbox(conn) if r["relayed_at"] is None]


def test_a_missing_job_is_terminally_relayed_with_no_event(conn, thread, caplog):
    _write(conn, job_id=9_999_999, kind="job.queued")

    assert relay.relay_outbox(conn) == 1

    assert _events(conn) == [] or all(e["source_kind"] != "outbox" for e in _events(conn))
    assert len(_relayed(conn)) == 1
    assert "is gone" in caplog.text


def test_a_non_conversation_job_is_relayed_with_no_event(conn, thread):
    other = _job(conn, source="jira", reference_id="PROJ-1")
    _write(conn, job_id=other, kind="job.queued")

    assert relay.relay_outbox(conn) == 1

    assert [e for e in _events(conn) if e["source_kind"] == "outbox"] == []
    assert len(_relayed(conn)) == 1


def test_a_non_conversation_reference_colliding_with_a_message_id_yields_no_event(
    conn, thread
):
    """Source first, never the reference value: an unrelated job may carry a reference
    that happens to read like a conversation's."""
    colliding = _job(conn, source="jira",
                     reference_id=f"{thread['conversation_id']}:{thread['message_id']}")
    _write(conn, job_id=colliding, kind="job.claimed")

    assert relay.relay_outbox(conn) == 1

    assert [e for e in _events(conn) if e["source_kind"] == "outbox"] == []
    assert len(_relayed(conn)) == 1


def test_a_missing_message_is_terminally_relayed_with_a_warning(conn, thread, caplog):
    orphan = _job(conn, source="conversation", reference_id="1:9999999")
    _write(conn, job_id=orphan, kind="job.claimed")

    assert relay.relay_outbox(conn) == 1

    assert [e for e in _events(conn) if e["source_kind"] == "outbox"] == []
    assert len(_relayed(conn)) == 1
    assert "does not exist" in caplog.text


def test_an_unusable_reference_is_terminally_relayed_with_a_warning(conn, thread, caplog):
    broken = _job(conn, source="conversation", reference_id="not-a-reference")
    _write(conn, job_id=broken, kind="job.claimed")

    assert relay.relay_outbox(conn) == 1

    assert [e for e in _events(conn) if e["source_kind"] == "outbox"] == []
    assert len(_relayed(conn)) == 1
    assert "unusable reference_id" in caplog.text


def test_the_conversation_is_taken_from_the_message_not_from_the_reference(conn, thread,
                                                                          world):
    """The reference's prefix is a convenience; the message row is the fact."""
    other_cid = service.create_conversation(conn, user_id=world["owner"].id,
                                            agent_view_id=world["view"], title="t2")
    lying = _job(conn, source="conversation",
                 reference_id=f"{other_cid}:{thread['message_id']}")
    _write(conn, job_id=lying, kind="job.claimed")

    relay.relay_outbox(conn)

    events = [e for e in _events(conn) if e["source_kind"] == "outbox"]
    assert [e["conversation_id"] for e in events] == [thread["conversation_id"]]


# --- the job.queued race (§4.1's two commits) -----------------------------

def test_a_tick_before_message_job_id_is_set_still_delivers_the_event(conn, thread):
    """The relay resolves by `message.id`, never by `message.job_id`: §4.1 sets that column
    in a second commit, so a tick in between would lose the first event of every thread."""
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_id = NULL WHERE id = %s",
                    (thread["message_id"],))
    conn.commit()
    _write(conn, job_id=thread["job_id"], kind="job.queued")

    assert relay.relay_outbox(conn) == 1

    assert [e["kind"] for e in _events(conn, thread["conversation_id"])
            if e["source_kind"] == "outbox"] == ["job.queued"]


# --- ordering, idempotency, interleaving ----------------------------------

def test_events_land_in_outbox_id_order_when_producers_interleave(conn, thread, world):
    second = service.create_conversation(conn, user_id=world["owner"].id,
                                         agent_view_id=world["view"], title="t2")
    _, second_job, _ = service.submit_message(
        conn, conversation_id=second, user_id=world["owner"].id,
        client_message_id="c2", content="drugie")

    # The second thread's own `job.queued` row is already in the outbox and is part of the
    # sequence: the order under test is the outbox's, not this test's.
    written = [r["id"] for r in _outbox(conn)] + [
        _write(conn, job_id=thread["job_id"], kind="job.queued"),
        _write(conn, job_id=second_job, kind="job.queued"),
        _write(conn, job_id=thread["job_id"], kind="job.claimed"),
        _write(conn, job_id=second_job, kind="job.claimed"),
    ]

    relay.relay_outbox(conn)

    events = [e for e in _events(conn) if e["source_kind"] == "outbox"]
    assert [e["source_id"] for e in events] == written
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)


def test_unrelated_rows_before_between_and_after_never_block_a_conversation_row(
    conn, thread
):
    other = _job(conn, source="jira", reference_id="PROJ-1")
    _write(conn, job_id=other, kind="job.queued")
    _write(conn, job_id=thread["job_id"], kind="job.queued")
    _write(conn, job_id=9_999_999, kind="job.claimed")
    _write(conn, job_id=thread["job_id"], kind="job.claimed")
    _write(conn, job_id=other, kind="job.failed")

    assert relay.relay_outbox(conn) == 5

    assert [e["kind"] for e in _events(conn, thread["conversation_id"])
            if e["source_kind"] == "outbox"] == ["job.queued", "job.claimed"]
    assert len(_relayed(conn)) == 5


def test_the_relay_is_idempotent_under_a_re_run(conn, thread):
    _write(conn, job_id=thread["job_id"], kind="job.claimed")
    relay.relay_outbox(conn)
    before = _events(conn)

    assert relay.relay_outbox(conn) == 0
    assert _events(conn) == before


def test_a_row_rewound_to_pending_yields_no_second_event(conn, thread):
    """A crash between the event insert and the mark re-runs the whole batch; the unique
    key is what absorbs it."""
    _write(conn, job_id=thread["job_id"], kind="job.claimed")
    relay.relay_outbox(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE job_event_outbox SET relayed_at = NULL")
    conn.commit()

    assert relay.relay_outbox(conn) == 1
    assert len([e for e in _events(conn) if e["source_kind"] == "outbox"]) == 1


def test_a_kill_between_the_transition_and_the_relay_still_yields_the_event(conn, thread):
    """The row is the durable part; no relay ever ran before this tick."""
    _write(conn, job_id=thread["job_id"], kind="job.failed",
           payload={"attempt": 1, "kind": "internal"})

    assert relay.relay_outbox(conn) == 1
    assert [e["kind"] for e in _events(conn, thread["conversation_id"])
            if e["source_kind"] == "outbox"] == ["job.failed"]


def test_the_execution_id_travels_onto_the_event(conn, thread):
    _write(conn, job_id=thread["job_id"], kind="execution.started",
           execution_id="exec-abc")

    relay.relay_outbox(conn)

    events = [e for e in _events(conn) if e["source_kind"] == "outbox"]
    assert [e["execution_id"] for e in events] == ["exec-abc"]


def test_a_batch_beyond_the_limit_leaves_the_rest_pending_in_order(conn, thread):
    ids = [_write(conn, job_id=thread["job_id"], kind="job.claimed") for _ in range(3)]

    assert relay.relay_outbox(conn, limit=2) == 2
    assert [r["id"] for r in _outbox(conn) if r["relayed_at"] is None] == ids[2:]

    assert relay.relay_outbox(conn, limit=2) == 1
    events = [e for e in _events(conn) if e["source_kind"] == "outbox"]
    assert [e["source_id"] for e in events] == ids


# --- the module disabled, and cleanup -------------------------------------

def test_with_the_module_disabled_the_transition_still_commits_and_is_never_relayed(
    conn, thread
):
    """Nothing here calls the relay: that is what "disabled" means for this table. The
    framework wrote its row regardless, and it is simply still pending."""
    row_id = _write(conn, job_id=thread["job_id"], kind="job.queued")

    pending = [r for r in _outbox(conn) if r["relayed_at"] is None]
    assert [r["id"] for r in pending] == [row_id]
    assert [e for e in _events(conn) if e["source_kind"] == "outbox"] == []


def test_prune_removes_relayed_rows_past_the_window_and_keeps_pending_ones(conn, thread):
    relayed = _write(conn, job_id=thread["job_id"], kind="job.claimed")
    relay.relay_outbox(conn)
    pending = _write(conn, job_id=thread["job_id"], kind="job.failed")
    with conn.cursor() as cur:
        cur.execute("UPDATE job_event_outbox SET created_at = NOW() - INTERVAL 30 DAY")
    conn.commit()

    assert relay.prune_relayed(conn, retention_days=7) == 1

    assert [r["id"] for r in _outbox(conn)] == [pending]
    assert relayed not in [r["id"] for r in _outbox(conn)]
    # The event outlives the row it came from: retention over events is §10.1's, not this.
    assert [e["source_id"] for e in _events(conn) if e["source_kind"] == "outbox"] \
        == [relayed]


def test_prune_leaves_a_relayed_row_inside_the_window(conn, thread):
    _write(conn, job_id=thread["job_id"], kind="job.claimed")
    relay.relay_outbox(conn)

    assert relay.prune_relayed(conn, retention_days=7) == 0
    assert len(_outbox(conn)) == 1
