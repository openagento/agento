"""The conversation lifecycle events and the observer manifest (PRD E3-E5 §6.4).

No lifecycle observer ships. That is EVT-7, not an omission: an event marking a state
change is the seam `app/code` and E7 extend, and every durable write here is elsewhere -
the `conversation_event` row and the outbox - so an observer can only ever be a reaction.
Each test therefore drives a test-local observer.
"""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from agento.framework.event_manager import ObserverEntry, clear, get_event_manager
from agento.framework.module_loader import import_class
from agento.modules.conversation.src import service

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _job_type  # noqa: F401

MODULE_DIR = Path(__file__).resolve().parents[2] / "src/agento/modules/conversation"


LIFECYCLE_EVENTS = (
    "conversation_create_after", "conversation_message_after",
    "conversation_archive_after", "conversation_reactivate_after",
    "conversation_delete_after",
)


def _spy(captured: list, name: str) -> type:
    """A fresh observer class per registration: the manager instantiates the CLASS on every
    dispatch, so what it captures has to live outside the instance."""
    return type("Spy", (), {
        "execute": lambda self, event: captured.append((name, event)),
    })


@pytest.fixture
def seen():
    """Every conversation event this test dispatches, in order."""
    clear()
    captured: list[tuple[str, object]] = []
    manager = get_event_manager()
    for name in LIFECYCLE_EVENTS:
        manager.register(name, ObserverEntry(name=f"spy_{name}",
                                             observer_class=_spy(captured, name)))
    yield captured
    clear()


def _only(seen, name) -> object:
    events = [e for n, e in seen if n == name]
    assert len(events) == 1, f"{name}: {len(events)} dispatched"
    return events[0]


# --- one test per event, asserting every documented field ----------------

def test_creating_a_thread_dispatches_its_ids(conn, world, seen):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="tytuł")

    assert asdict(_only(seen, "conversation_create_after")) == {
        "conversation_id": conversation_id,
        "agent_view_id": world["view"],
        "user_id": world["owner"].id,
    }


def test_a_viewless_thread_dispatches_a_null_view(conn, world, seen):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=None, title=None)

    assert asdict(_only(seen, "conversation_create_after")) == {
        "conversation_id": conversation_id, "agent_view_id": None,
        "user_id": world["owner"].id,
    }


def test_a_submitted_turn_dispatches_after_the_job_exists(conn, world, seen):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=conversation_id, user_id=world["owner"].id,
        client_message_id="c1", content="pytanie")

    event = _only(seen, "conversation_message_after")
    assert asdict(event) == {"conversation_id": conversation_id,
                             "message_id": message_id, "job_id": job_id}
    # Dispatched after step 3, so the id it carries is a job that exists.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM job WHERE id = %s", (event.job_id,))
        assert cur.fetchone() is not None
    conn.commit()


def test_a_replayed_submission_announces_nothing_more(conn, world, seen):
    """An event announces a transition that HAPPENED (EVT-4). A replay accepts no message and
    publishes no job, so the one event belongs to the call that did both - and it carries the
    same ids the replay hands back."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    first = service.submit_message(conn, conversation_id=conversation_id,
                                   user_id=world["owner"].id, client_message_id="c1",
                                   content="pytanie")
    second = service.submit_message(conn, conversation_id=conversation_id,
                                    user_id=world["owner"].id, client_message_id="c1",
                                    content="pytanie")

    assert first[:2] == second[:2]
    events = [e for n, e in seen if n == "conversation_message_after"]
    assert len(events) == 1
    assert (events[0].message_id, events[0].job_id) == first[:2]


def test_archiving_by_hand_dispatches_the_actor_and_the_manual_reason(conn, world, seen):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")

    service.archive(conn, conversation_id, actor_id=world["owner"].id, reason="manual")

    assert asdict(_only(seen, "conversation_archive_after")) == {
        "conversation_id": conversation_id, "actor_id": world["owner"].id,
        "reason": "manual",
    }


def test_retiring_an_idle_thread_dispatches_the_same_event_with_no_actor(conn, world, seen):
    """One event for both paths, because there is one archive function. `actor_id` is None
    exactly when no human did it."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")

    service.archive(conn, conversation_id, reason="idle")

    assert asdict(_only(seen, "conversation_archive_after")) == {
        "conversation_id": conversation_id, "actor_id": None, "reason": "idle",
    }


def test_reactivating_dispatches_the_actor(conn, world, seen):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    service.archive(conn, conversation_id, actor_id=world["owner"].id)

    service.reactivate(conn, conversation_id, actor_id=world["owner"].id)

    assert asdict(_only(seen, "conversation_reactivate_after")) == {
        "conversation_id": conversation_id, "actor_id": world["owner"].id,
    }
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM conversation WHERE id = %s", (conversation_id,))
        assert cur.fetchone()["status"] == "active"
    conn.commit()


def test_deleting_dispatches_after_the_row_is_already_gone(conn, world, seen):
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")

    service.delete_conversation(conn, conversation_id)

    assert asdict(_only(seen, "conversation_delete_after")) == {
        "conversation_id": conversation_id,
    }
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM conversation WHERE id = %s", (conversation_id,))
        assert cur.fetchone() is None
    conn.commit()


def test_every_event_is_dispatched_after_its_commit(conn, world, seen):
    """An observer is fail-open (F22) and may not sit inside the transaction the thread's
    durability depends on. Each observer here reads the state on its OWN connection: a row
    it can see from outside is a row that committed."""
    from .conftest import _test_connection

    outside = _test_connection(autocommit=True)
    statuses: list[object] = []

    def read(self, event):
        with outside.cursor() as cur:
            cur.execute("SELECT status FROM conversation WHERE id = %s",
                        (event.conversation_id,))
            row = cur.fetchone()
        statuses.append(row["status"] if row else None)

    Reader = type("Reader", (), {"execute": read})
    manager = get_event_manager()
    for name in ("conversation_create_after", "conversation_archive_after",
                 "conversation_delete_after"):
        manager.register(name, ObserverEntry(name=f"reader_{name}", observer_class=Reader))
    try:
        conversation_id = service.create_conversation(
            conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
        service.archive(conn, conversation_id, actor_id=world["owner"].id)
        service.delete_conversation(conn, conversation_id)
    finally:
        outside.close()

    assert statuses == ["active", "archived", None]


# --- EVT-3: ids, never content -------------------------------------------

def test_no_lifecycle_payload_carries_content_a_config_value_or_external_input(
    conn, world, seen
):
    """One assertion over every new payload. A title and a message body are raw user text;
    an event carrying either would put external input in front of every observer of every
    thread."""
    title = "TYTUŁ-Z-ZEWNĄTRZ"
    content = "TREŚĆ-Z-ZEWNĄTRZ"
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title=title)
    service.submit_message(conn, conversation_id=conversation_id,
                           user_id=world["owner"].id, client_message_id="c1",
                           content=content)
    service.archive(conn, conversation_id, actor_id=world["owner"].id)
    service.reactivate(conn, conversation_id, actor_id=world["owner"].id)
    service.delete_conversation(conn, conversation_id)

    assert {name for name, _ in seen} == {
        "conversation_create_after", "conversation_message_after",
        "conversation_archive_after", "conversation_reactivate_after",
        "conversation_delete_after",
    }
    for name, event in seen:
        payload = asdict(event)
        rendered = json.dumps(payload, ensure_ascii=False)
        assert title not in rendered and content not in rendered, name
        assert "CONFIG__" not in rendered, name
        # Every field is an id, a null, or the closed `reason` vocabulary.
        for key, value in payload.items():
            if key == "reason":
                assert value in ("manual", "idle")
            else:
                assert value is None or isinstance(value, int), f"{name}.{key} = {value!r}"


def test_the_unblock_event_carries_ids_only():
    """Task 18 dispatches it; the payload contract is fixed here with the rest."""
    from agento.framework.events import ConversationUnblockedEvent

    assert [f.name for f in fields(ConversationUnblockedEvent)] == [
        "conversation_id", "message_id", "actor_id"]


# --- the observer manifest ------------------------------------------------

def test_events_json_carries_the_ordering_observer_and_nothing_else():
    """A regression test for the round that 'tidies' the manifest. `job_claim_before` is
    §4.4's ordering veto - the one observer this epic ships, and the only one that may be
    an observer at all, because a veto is a decision and not a reaction."""
    manifest = json.loads((MODULE_DIR / "events.json").read_text())

    assert list(manifest) == ["job_claim_before"]
    assert [r["name"] for r in manifest["job_claim_before"]] == [
        "conversation_turn_ordering"]


def test_every_class_path_in_the_manifest_resolves():
    manifest = json.loads((MODULE_DIR / "events.json").read_text())

    for registrations in manifest.values():
        for registration in registrations:
            observer = import_class(MODULE_DIR, registration["class"])
            assert callable(getattr(observer, "execute", None)) or hasattr(observer, "execute")


def test_no_lifecycle_observer_is_registered_in_the_manifest():
    """EVT-7: nothing in-tree observes these events, on purpose. A 'bookkeeping' observer
    with no stated behaviour would be dead code (CODE-4)."""
    manifest = json.loads((MODULE_DIR / "events.json").read_text())

    assert not [name for name in manifest if name.startswith("conversation_")]


def test_the_sweep_announces_the_turn_it_publishes(conn, world, seen):
    """EVT-2, the other half: the event follows the PUBLISH, wherever it happens. A message
    left `pending` by a crash is published by §4.4's sweep, and that is a real transition -
    the thread must not stay silent about the turn that is now running."""
    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO message (conversation_id, role, content, "
                    "client_message_id, job_state) VALUES (%s, 'user', 'pytanie', "
                    "'c-sweep', 'pending')", (conversation_id,))
        message_id = cur.lastrowid
    conn.commit()
    seen.clear()

    assert service.sweep_pending(conn, grace_seconds=0) == 1

    events = [e for n, e in seen if n == "conversation_message_after"]
    assert len(events) == 1 and events[0].message_id == message_id
    assert events[0].job_id is not None


def test_two_callers_completing_one_pending_turn_announce_it_once(conn, world, seen,
                                                                  monkeypatch):
    """EVT-4: one transition, one event. The route and §4.4's sweep can both find the same
    `pending` message; the conditional UPDATE decides which one made the transition, and only
    that one announces it. The loser still answers with the stored job id."""
    from .conftest import _test_connection

    conversation_id = service.create_conversation(
        conn, user_id=world["owner"].id, agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO message (conversation_id, role, content, "
                    "client_message_id, job_state) VALUES (%s, 'user', 'pytanie', "
                    "'c-race', 'pending')", (conversation_id,))
        message_id = cur.lastrowid
    conn.commit()
    seen.clear()

    # The interleaving that matters: BOTH callers read `pending`, so both reach the UPDATE.
    # The second one runs from inside the first one's publish, which is the window itself.
    done: list[int] = []
    raced = []
    original = service.publish_job

    def racing(**kwargs):
        if not raced:
            raced.append(True)
            done.append(service.complete_pending(other, message_id))
        return original(**kwargs)

    other = _test_connection()
    try:
        monkeypatch.setattr(service, "publish_job", racing)
        first = service.complete_pending(conn, message_id)
    finally:
        monkeypatch.setattr(service, "publish_job", original)
        other.close()

    assert first == done[0]
    assert len([e for n, e in seen if n == "conversation_message_after"]) == 1
