"""One conversation runs one turn at a time (PRD E3-E5 §4.4).

The rule is the module's; the seam is the framework's. These tests drive the real consumer
so that a rule which works in a unit test but never reaches `_try_dequeue` still fails.
"""
from __future__ import annotations

import logging

import pytest

from agento.framework.consumer import Consumer
from agento.framework.event_manager import get_event_manager
from agento.framework.job_types import clear_job_types, register_job_type
from agento.modules.conversation.src import service
from agento.modules.conversation.src.hooks import ConversationOrderingObserver

from .conftest import (
    _clean,  # noqa: F401
    bootstrap_for_tests,
)


@pytest.fixture(autouse=True)
def _registered():
    bootstrap_for_tests()
    register_job_type("conversation", module="conversation")
    yield
    clear_job_types()


def _consumer(int_db_config, int_consumer_config) -> Consumer:
    return Consumer(int_db_config, int_consumer_config, logging.getLogger("test"))


def _thread(conn, world, title="t") -> int:
    return service.create_conversation(conn, user_id=world["owner"].id,
                                       agent_view_id=world["view"], title=title)


def _send(conn, cid, world, client_message_id, content) -> tuple[int, int]:
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["owner"].id,
        client_message_id=client_message_id, content=content)
    return message_id, job_id


def _finish(conn, message_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_state = 'terminal' WHERE id = %s", (message_id,))
    conn.commit()


def _release(conn) -> None:
    """Undo the backoff so the next poll tick sees the deferred job again."""
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET scheduled_after = NOW() WHERE status = 'TODO'")
    conn.commit()


def test_the_observer_is_reached_through_bootstrap(conn, world):
    """Through events.json, not by hand: a missing declaration must fail this test."""
    observers = get_event_manager()._observers.get("job_claim_before", [])

    assert ConversationOrderingObserver in [o.observer_class for o in observers]


def test_two_turns_of_one_thread_are_claimed_in_order(
    conn, world, int_db_config, int_consumer_config
):
    cid = _thread(conn, world)
    first_msg, first_job = _send(conn, cid, world, "c1", "pytanie jeden")
    _, second_job = _send(conn, cid, world, "c2", "pytanie dwa")
    consumer = _consumer(int_db_config, int_consumer_config)

    assert consumer._try_dequeue().id == first_job    # the older turn goes first
    _release(conn)
    assert consumer._try_dequeue() is None            # the younger one waits for it

    _finish(conn, first_msg)
    _release(conn)

    assert consumer._try_dequeue().id == second_job


def test_a_deferred_turn_does_not_hold_another_thread(
    conn, world, int_db_config, int_consumer_config
):
    """The rule is per conversation: two threads run concurrently, as they must."""
    blocked = _thread(conn, world, title="blocked")
    _send(conn, blocked, world, "b1", "pierwsze")
    _, second_of_blocked = _send(conn, blocked, world, "b2", "drugie")
    other = _thread(conn, world, title="other")
    _, other_job = _send(conn, other, world, "o1", "inne pytanie")
    consumer = _consumer(int_db_config, int_consumer_config)

    consumer._try_dequeue()                            # blocked's first turn
    _release(conn)
    claimed = [consumer._try_dequeue(), consumer._try_dequeue()]

    ids = [j.id for j in claimed if j is not None]
    assert other_job in ids
    assert second_of_blocked not in ids


def test_a_blocked_turn_leaves_one_stretch_and_one_event(
    conn, world, int_db_config, int_consumer_config
):
    cid = _thread(conn, world)
    first_msg, first_job = _send(conn, cid, world, "c1", "pytanie jeden")
    _, second_job = _send(conn, cid, world, "c2", "pytanie dwa")
    consumer = _consumer(int_db_config, int_consumer_config)
    assert consumer._try_dequeue().id == first_job

    for _ in range(3):
        _release(conn)
        assert consumer._try_dequeue() is None

    with conn.cursor() as cur:
        cur.execute("SELECT defer_count, closed_at FROM job_defer_stretch "
                    "WHERE job_id = %s", (second_job,))
        assert [dict(r) for r in cur.fetchall()] == [{"defer_count": 3, "closed_at": None}]
        cur.execute("SELECT kind FROM job_event_outbox WHERE kind = 'job.deferred' "
                    "AND job_id = %s", (second_job,))
        assert cur.fetchall() == ()
    conn.commit()

    _finish(conn, first_msg)
    _release(conn)
    assert consumer._try_dequeue() is not None

    with conn.cursor() as cur:
        cur.execute("SELECT payload FROM job_event_outbox WHERE kind = 'job.deferred' "
                    "AND job_id = %s", (second_job,))
        rows = cur.fetchall()
    conn.commit()
    import json
    assert len(rows) == 1 and json.loads(rows[0]["payload"])["defer_count"] == 3


def test_an_unusable_reference_is_claimed_not_held_forever(
    conn, world, int_db_config, int_consumer_config
):
    """A reference the module cannot read is the workflow's problem, not the queue's."""
    _thread(conn, world)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job (type, source, reference_id, idempotency_key, status) "
                    "VALUES ('conversation', 'test', 'nonsense', 'ord:bad', 'TODO')")
        job_id = cur.lastrowid
    conn.commit()

    claimed = _consumer(int_db_config, int_consumer_config)._try_dequeue()

    assert claimed is not None and claimed.id == job_id
