"""A conversation is the timeline of one thread (plan §9, migration 007).

Every job of a thread - each comment round on a Jira issue, each mail of a mail conversation,
a panel reply, a sub-agent in any view - runs in the thread's one conversation, so the panel
reads the turns of the thread and the sub-agents nested under their parent's turn.
"""
from __future__ import annotations

import json
import uuid

import pytest

from agento.framework.job_models import Job, JobRequester, RequesterTrust
from agento.framework.job_types import resolve_job_type
from agento.framework.publish_service import publish_job
from agento.framework.threads import place_claim
from agento.modules.conversation.src import routes, service
from agento.modules.conversation.src.hooks import ConversationExecutionIds

from .test_conversation_submission import _Req


def _rows(conn, sql, args=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        rows = list(cur.fetchall())
    conn.commit()
    return rows


def _publish(*, view, source="jira", reference="AI-7", thread=None, thread_id=None,
             parent_job_id=None, depth=0) -> int:
    return publish_job(source=source, agent_type=resolve_job_type("followup"), agent_view_id=view,
                       reference_id=reference, idempotency_key=str(uuid.uuid4()),
                       requester=JobRequester(key="t", trust=RequesterTrust.CLAIMED),
                       priority=50, thread=thread, thread_id=thread_id,
                       parent_job_id=parent_job_id, depth=depth)


def _run(conn, job_id: int) -> int | None:
    """Mint the run as the claim does; returns the conversation it landed in."""
    execution_id = ConversationExecutionIds().mint(conn=conn, job_id=job_id, attempt=1)
    conn.commit()
    found = _rows(conn, "SELECT conversation_id FROM execution WHERE execution_id = %s",
                  (execution_id,))
    return found[0]["conversation_id"]


def _job(conn, job_id: int) -> dict:
    return _rows(conn, "SELECT * FROM job WHERE id = %s", (job_id,))[0]


def _started(conn, conversation_id: int) -> list[dict]:
    return [json.loads(r["payload"]) for r in _rows(
        conn, "SELECT payload FROM conversation_event WHERE conversation_id = %s "
              "AND kind = 'run.started' ORDER BY id", (conversation_id,))]


# --- one conversation per thread ---------------------------------------------------

def test_a_panel_conversation_is_created_with_its_own_thread(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")

    row = _rows(conn, "SELECT t.ref, t.agent_view_id FROM conversation c "
                      "JOIN thread t ON t.id = c.thread_id WHERE c.id = %s", (cid,))[0]
    assert (row["ref"], row["agent_view_id"]) == (f"conversation:{cid}", world["view"])


def test_seven_rounds_on_one_issue_are_one_conversation_with_seven_turns(conn, world):
    """Comments and panel replies alternate; every round is a turn of the issue's thread."""
    cid = _run(conn, _publish(view=world["view"]))
    for n in range(6):
        if n % 2:
            sent = routes.send(_Req(conn, world["admin"], params={"id": str(cid)},
                                    json={"client_message_id": f"r{n}", "content": "ok"}))
            job_id = sent.body["job_id"]
        else:
            job_id = _publish(view=world["view"])
        assert _run(conn, job_id) == cid

    assert len(_started(conn, cid)) == 7
    assert len(_rows(conn, "SELECT id FROM conversation")) == 1
    shown = routes.show(_Req(conn, world["admin"], params={"id": str(cid)}))
    assert len(shown.body["runs"]) == 7


def test_a_panel_reply_to_a_mail_conversation_lands_in_its_thread(conn, world):
    """The mail conversation's thread is not the default thread of any one mail: the reply
    joins it by id, and answers the newest mail of the conversation."""
    ref = "outlook:conversation:C1"
    cid = _run(conn, _publish(view=world["view"], source="outlook", reference="m-1", thread=ref))
    assert _run(conn, _publish(view=world["view"], source="outlook", reference="m-2",
                               thread=ref)) == cid

    sent = routes.send(_Req(conn, world["admin"], params={"id": str(cid)},
                            json={"client_message_id": "r1", "content": "odpowiedź"}))

    reply = _job(conn, sent.body["job_id"])
    conversation = _rows(conn, "SELECT thread_id, external_ref FROM conversation "
                               "WHERE id = %s", (cid,))[0]
    assert reply["thread_id"] == conversation["thread_id"]
    assert (reply["source"], reply["reference_id"]) == ("outlook", "m-2")
    assert _run(conn, reply["id"]) == cid


def test_a_reply_the_claim_detaches_still_answers_in_the_conversation_it_was_posted_in(
        conn, world):
    """`OutlookThreadGate` moves a reply to its own mail's thread when thread read is off;
    the admin still gets the answer where they posted it."""
    cid = _run(conn, _publish(view=world["view"], source="outlook", reference="m-1",
                              thread="outlook:conversation:C1"))
    sent = routes.send(_Req(conn, world["admin"], params={"id": str(cid)},
                            json={"client_message_id": "r1", "content": "odpowiedź"}))
    job_id = sent.body["job_id"]
    with conn.cursor() as cur:            # the claim of a gate that set `detach_thread`
        place_claim(cur, Job.from_row(_job(conn, job_id)), detach=True)
    conn.commit()
    assert _job(conn, job_id)["thread_id"] != _rows(
        conn, "SELECT thread_id FROM conversation WHERE id = %s", (cid,))[0]["thread_id"]

    assert _run(conn, job_id) == cid
    assert len(_rows(conn, "SELECT id FROM conversation")) == 1


def test_a_sub_agent_in_another_view_runs_in_its_parents_conversation(conn, world):
    parent = _publish(view=world["view"])
    cid = _run(conn, parent)
    child = _publish(view=world["other_view"], source="subagent", reference=None,
                     thread_id=_job(conn, parent)["thread_id"], parent_job_id=parent, depth=1)
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET resume_job_id = %s WHERE id = %s", (parent, child))
    conn.commit()

    assert _run(conn, child) == cid

    started = _started(conn, cid)[-1]
    assert (started["job_id"], started["parent_job_id"], started["depth"],
            started["resume_job_id"], started["agent_view"]) == (child, parent, 1, parent,
                                                                  "c-av2")
    # The child opened no conversation of its own view, and the parent's keeps its view.
    assert [r["agent_view_id"] for r in _rows(conn, "SELECT agent_view_id FROM conversation")] \
        == [world["view"]]
    runs = routes.show(_Req(conn, world["admin"], params={"id": str(cid)})).body["runs"]
    assert {(r["job_id"], r["parent_job_id"], r["depth"], r["agent_view"]) for r in runs} == {
        (parent, None, 0, "c-av"), (child, parent, 1, "c-av2")}


def test_a_sub_agent_of_another_view_shows_to_the_parents_readers_gated_by_its_run_details(
        conn, world):
    """Owner decision (plan §15 S5): a child that runs in view B shows in view A's
    conversation to A's readers. Which view it ran in, and what it resumed, are run details:
    A's `conversation.run_details` shows them; a grant on B does not."""
    from agento.framework.access import accounts

    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, parent, _ = service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                                          client_message_id="m1", content="hello")
    assert _run(conn, parent) == cid
    child = _publish(view=world["other_view"], source="subagent", reference=str(parent),
                     thread_id=_job(conn, parent)["thread_id"], parent_job_id=parent, depth=1)
    assert _run(conn, child) == cid

    def seen() -> tuple[list[dict], dict]:
        req = _Req(conn, world["owner"], params={"id": str(cid)})
        runs = routes.show(req).body["runs"]
        started = [e["payload"] for e in routes.timeline(req).body["events"]
                   if e["kind"] == "run.started"]
        return runs, started[-1]

    runs, started = seen()
    assert len(runs) == 2 and all("agent_view" not in r and "job_id" not in r for r in runs)
    # The rail's tree is for every reader: the child's id, parent, depth and status.
    jobs = routes.show(_Req(conn, world["owner"], params={"id": str(cid)})).body["jobs"]
    assert [(j["id"], j["parent_job_id"], j["depth"]) for j in jobs] == [(child, parent, 1)]
    assert set(jobs[0]) == {"id", "parent_job_id", "depth", "status"}
    assert (started["job_id"], started["parent_job_id"], started["depth"]) == (child, parent, 1)
    assert "agent_view" not in started and "resume_job_id" not in started

    accounts.add_grant(conn, "user", "operation", service.RUN_DETAILS,
                       agent_view_id=world["other_view"])
    runs, started = seen()
    assert all("agent_view" not in r for r in runs) and "agent_view" not in started

    accounts.add_grant(conn, "user", "operation", service.RUN_DETAILS,
                       agent_view_id=world["view"])
    runs, started = seen()
    assert {(r["job_id"], r["agent_view"]) for r in runs} == {(parent, "c-av"),
                                                              (child, "c-av2")}
    assert started["agent_view"] == "c-av2" and "resume_job_id" in started


def test_a_run_in_a_deleted_panel_conversation_opens_no_ownerless_one(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, job_id, _ = service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                                          client_message_id="m1", content="hello")
    service.delete_conversation(conn, cid)

    assert _run(conn, job_id) is None
    assert _rows(conn, "SELECT id FROM conversation") == []


def test_a_run_lands_in_its_archived_panel_conversation_and_leaves_it_archived(conn, world):
    """Only a user's post revives a user's conversation (`reactivate`), never a run."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, job_id, _ = service.submit_message(conn, conversation_id=cid, user_id=world["owner"].id,
                                          client_message_id="m1", content="hello")
    service.archive(conn, cid, actor_id=None, reason="idle")

    assert _run(conn, job_id) == cid
    assert _rows(conn, "SELECT status FROM conversation WHERE id = %s", (cid,))[0]["status"] \
        == "archived"


@pytest.mark.parametrize("source,depth", [
    pytest.param("github", 0, id="another-source"),
    pytest.param("jira", 1, id="a-sub-agent"),
])
def test_only_a_depth_0_run_of_the_channel_moves_the_reply_target(conn, world, source, depth):
    first = _publish(view=world["view"])
    cid = _run(conn, first)
    _run(conn, _publish(view=world["view"], source=source, reference="OTHER-1",
                        thread_id=_job(conn, first)["thread_id"], depth=depth,
                        parent_job_id=first if depth else None))

    assert _rows(conn, "SELECT external_ref FROM conversation WHERE id = %s",
                 (cid,))[0]["external_ref"] == "AI-7"


def test_a_pending_reply_whose_view_is_gone_is_still_published_by_the_sweep(conn, world):
    """A thread with no view has no owner to join: the reply gets its default thread, and
    the one row does not stop the sweep."""
    cid = _run(conn, _publish(view=world["other_view"]))
    message_id, _, _ = service.submit_message(conn, conversation_id=cid,
                                              user_id=world["admin"].id,
                                              client_message_id="r1", content="ok")
    with conn.cursor() as cur:
        cur.execute("UPDATE message SET job_state = 'pending', job_id = NULL WHERE id = %s",
                    (message_id,))
        cur.execute("DELETE FROM job WHERE idempotency_key LIKE 'channel-reply:%%'")
        cur.execute("DELETE FROM agent_view WHERE id = %s", (world["other_view"],))
    conn.commit()

    assert service.sweep_pending(conn, grace_seconds=0) == 1

    job_id = _rows(conn, "SELECT job_id FROM message WHERE id = %s", (message_id,))[0]["job_id"]
    reply = _job(conn, job_id)
    conversation = _rows(conn, "SELECT thread_id FROM conversation WHERE id = %s", (cid,))[0]
    assert (reply["source"], reply["reference_id"]) == ("jira", "AI-7")
    assert reply["thread_id"] is not None and reply["thread_id"] != conversation["thread_id"]
