"""The conversation channel and workflow (PRD E3-E5 §4.2).

The prompt is bounded by `reference_id`, not by "everything in the thread": a second turn
queued while the first waits to be claimed must not be answered by the first turn's run.
"""
from __future__ import annotations

import logging

import pytest

from agento.framework.channels.registry import get_channel
from agento.framework.job_types import clear_job_types, register_job_type
from agento.framework.workflows import clear as clear_workflows
from agento.framework.workflows import get_workflow_class
from agento.modules.conversation.src import service
from agento.modules.conversation.src.channel import ConversationChannel
from agento.modules.conversation.src.workflow import (
    ConversationWorkflow,
    ReferenceUnusable,
    parse_reference,
)

from .conftest import (
    _clean,  # noqa: F401
    _test_connection,
    bootstrap_for_tests,
)


@pytest.fixture(autouse=True)
def _job_type():
    register_job_type("conversation", module="conversation")
    yield
    clear_job_types()


def _prompt(conn, reference_id: str) -> str:
    workflow = ConversationWorkflow(runner=None, logger=logging.getLogger("test"))
    return workflow.build_prompt(ConversationChannel(), reference_id, conn=conn)


def test_the_prompt_carries_the_turn_and_the_prior_history(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO message (conversation_id, role, content) "
                    "VALUES (%s, 'user', 'pierwsze'), (%s, 'assistant', 'odpowiedź')", (cid, cid))
    conn.commit()
    message_id, _, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["owner"].id,
        client_message_id="c1", content="drugie")

    prompt = _prompt(conn, f"{cid}:{message_id}")

    assert prompt.index("pierwsze") < prompt.index("odpowiedź") < prompt.index("drugie")
    assert "Użytkownik: drugie" in prompt


def test_a_second_turn_queued_first_is_not_swallowed_by_the_first_run(conn, world):
    """Two submissions before either is claimed: turn 1's prompt stops at turn 1."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    first, _, _ = service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                                         client_message_id="c1", content="pytanie jeden")
    second, _, _ = service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                                          client_message_id="c2", content="pytanie dwa")

    prompt = _prompt(conn, f"{cid}:{first}")

    assert "pytanie jeden" in prompt
    assert "pytanie dwa" not in prompt
    assert "pytanie dwa" in _prompt(conn, f"{cid}:{second}")


@pytest.mark.parametrize("reference_id", [None, "", "7", "7:", ":9", "abc:9", "7:abc", "7:9:11"])
def test_an_unusable_reference_id_fails_closed(reference_id):
    with pytest.raises(ReferenceUnusable) as exc:
        parse_reference(reference_id)

    assert repr(reference_id) in str(exc.value)
    assert "<conversation id>:<message id>" in str(exc.value)


def test_an_unknown_message_fails_closed(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")

    with pytest.raises(ReferenceUnusable):
        _prompt(conn, f"{cid}:999999")


def test_a_valid_reference_parses_to_two_ids():
    assert parse_reference("12:34") == (12, 34)


def test_the_module_registers_its_channel_and_workflow():
    """Through bootstrap, not by hand: a missing di.json entry must fail this test."""
    clear_workflows()
    bootstrap_for_tests()

    assert get_channel("conversation").name == "conversation"
    assert get_workflow_class("conversation") is ConversationWorkflow


def test_a_posted_message_produces_a_job_the_consumer_runs(
    int_db_config, int_consumer_config, mock_claude, int_agent_view
):
    """End to end: submit → publish → claim → run, with the thread in the prompt."""
    import logging as _logging

    from agento.framework.access import accounts
    from agento.framework.consumer import Consumer

    bootstrap_for_tests()   # production runs the consumer after a bootstrap; so does this

    from .conftest import fetch_all_jobs, fetch_job

    c = _test_connection(autocommit=False)
    try:
        with c.cursor() as cur:
            for table in ("conversation_event", "message", "conversation", "job",
                          "session", "role_grant", "`user`"):
                cur.execute(f"DELETE FROM {table}")
        c.commit()
        owner = accounts.create_user(c, "e2e-owner", "user", "pw-owner-1234")
        cid = service.create_conversation(c, user_id=owner.id, agent_view_id=int_agent_view,
                                          title="e2e")
        message_id, job_id, _ = service.submit_message(
            c, conversation_id=cid, user_id=owner.id,
            client_message_id="e2e-1", content="jakie jest pytanie")
    finally:
        c.close()

    jobs = fetch_all_jobs()
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["type"] == "conversation"
    assert jobs[0]["reference_id"] == f"{cid}:{message_id}"

    consumer = Consumer(int_db_config, int_consumer_config, _logging.getLogger("test"))
    job = consumer._try_dequeue()
    assert job is not None and job.id == job_id
    consumer._execute_job(job)

    row = fetch_job(job_id)
    assert row["status"] == "SUCCESS"
    assert "jakie jest pytanie" in row["prompt"]
