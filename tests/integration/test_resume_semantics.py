"""§5.2's three cases, each asserted on what the harness actually received.

A retry resumes the interrupted run. A follow-up on a resumable harness resumes the thread's
session and is sent the new turn alone. A follow-up on a harness that cannot resume gets a
fresh session and the assembled history. The bound on that history is `message.id <=
reference_id` in all three cases.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from agento.framework.consumer import _should_resume
from agento.framework.execution_hooks import clear as clear_hooks
from agento.framework.execution_hooks import (
    register_resume_session_resolver,
    resolve_resume_session,
)
from agento.framework.harness import RunRequest, RunResult
from agento.framework.workflows.base import JobContext
from agento.modules.conversation.src import routes, service
from agento.modules.conversation.src.channel import ConversationChannel
from agento.modules.conversation.src.hooks import ConversationResumeSessions
from agento.modules.conversation.src.workflow import ConversationWorkflow

# `_job_type` is autouse in its own module and does NOT travel with the fixtures imported
# from it; without it every publish here raises JobTypeUnknown once the registry is clear.
from .conftest import (
    _clean,  # noqa: F401
    _test_connection,
    bootstrap_for_tests,
)
from .test_conversation_submission import _job_type, _Req  # noqa: F401


class SpyRunner:
    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    def execute(self, request: RunRequest) -> RunResult:
        self.requests.append(request)
        return RunResult(raw_output="ok", session_id="new-session", harness="test")

    def observe(self, **kwargs) -> None:
        pass


@pytest.fixture
def resolver():
    clear_hooks()
    register_resume_session_resolver(ConversationResumeSessions(), module="conversation")
    yield
    # The registry is global: leave the shipped registrations behind us, or every
    # later test in the session runs with the seams empty.
    clear_hooks()
    bootstrap_for_tests()


def _turn(conn, cid, world, client_message_id, content) -> tuple[int, int]:
    message_id, job_id, _ = service.submit_message(
        conn, conversation_id=cid, user_id=world["owner"].id,
        client_message_id=client_message_id, content=content)
    return message_id, job_id


def _answer(conn, cid, job_id, *, session_id, text="odpowiedź") -> None:
    """Close a turn the way a finished run does: a session on the job, an assistant reply."""
    with conn.cursor() as cur:
        cur.execute("UPDATE job SET session_id = %s, status = 'SUCCESS' WHERE id = %s",
                    (session_id, job_id))
        cur.execute("INSERT INTO message (conversation_id, role, content) "
                    "VALUES (%s, 'assistant', %s)", (cid, text))
    conn.commit()


def _run(conn, job_id, reference_id, *, resume: str | None) -> RunRequest:
    runner = SpyRunner()
    workflow = ConversationWorkflow(runner=runner, logger=logging.getLogger("test"))

    class _Job:
        id = job_id
        reference_id_ = reference_id

    job = type("J", (), {"id": job_id, "reference_id": reference_id})()
    context = JobContext(config={}, logger=logging.getLogger("test"),
                         update_reference_id=lambda *a: None, resume_session_id=resume)
    # `build_prompt` closes the connection it opens, so it gets a throwaway one - the
    # test's own connection must survive the call.
    import agento.modules.conversation.src.workflow as wf
    original = wf.get_connection
    wf.get_connection = lambda *_a, **_k: _test_connection(autocommit=True)
    try:
        workflow.execute_job(ConversationChannel(), job, context)
    finally:
        wf.get_connection = original
    return runner.requests[0]


# --- case 1: a retry resumes the interrupted run, not the previous turn ----

def test_a_retry_is_the_frameworks_own_rule_and_the_resolver_declines(
    conn, world, resolver
):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, first_job = _turn(conn, cid, world, "c1", "pytanie jeden")
    _answer(conn, cid, first_job, session_id="session-one")
    _, second_job = _turn(conn, cid, world, "c2", "pytanie dwa")

    assert resolve_resume_session(conn=conn, job_id=second_job, attempt=2) is None


def test_the_frameworks_retry_rule_is_unchanged():
    assert _should_resume(attempt=2, session_id="s", pid_alive=False, can_resume=True)
    assert not _should_resume(attempt=1, session_id="s", pid_alive=False, can_resume=True)
    assert not _should_resume(attempt=2, session_id="s", pid_alive=False, can_resume=False)


# --- case 2: a follow-up on a resumable harness ---------------------------

def test_a_follow_up_resolves_the_previous_turns_session(conn, world, resolver):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, first_job = _turn(conn, cid, world, "c1", "pytanie jeden")
    _answer(conn, cid, first_job, session_id="session-one")
    _, second_job = _turn(conn, cid, world, "c2", "pytanie dwa")

    assert resolve_resume_session(
        conn=conn, job_id=second_job, attempt=1) == "session-one"


def test_a_resumed_follow_up_is_sent_the_new_turn_alone(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, first_job = _turn(conn, cid, world, "c1", "pytanie jeden")
    _answer(conn, cid, first_job, session_id="session-one")
    second_msg, second_job = _turn(conn, cid, world, "c2", "pytanie dwa")

    request = _run(conn, second_job, f"{cid}:{second_msg}", resume="session-one")

    assert request.session_id == "session-one"
    assert "pytanie dwa" in request.prompt
    assert "pytanie jeden" not in request.prompt
    assert "odpowiedź" not in request.prompt


def test_the_first_turn_of_a_thread_resolves_nothing(conn, world, resolver):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, job_id = _turn(conn, cid, world, "c1", "pytanie jeden")

    assert resolve_resume_session(conn=conn, job_id=job_id, attempt=1) is None


def test_a_turn_whose_predecessor_never_got_a_session_resolves_nothing(
    conn, world, resolver
):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _turn(conn, cid, world, "c1", "pytanie jeden")
    _, second_job = _turn(conn, cid, world, "c2", "pytanie dwa")

    assert resolve_resume_session(conn=conn, job_id=second_job, attempt=1) is None


# --- case 3: a fresh session gets the assembled history --------------------

def test_a_fresh_follow_up_is_sent_the_assembled_history(conn, world):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, first_job = _turn(conn, cid, world, "c1", "pytanie jeden")
    _answer(conn, cid, first_job, session_id="session-one")
    second_msg, second_job = _turn(conn, cid, world, "c2", "pytanie dwa")

    request = _run(conn, second_job, f"{cid}:{second_msg}", resume=None)

    assert request.session_id is None
    assert "pytanie jeden" in request.prompt
    assert "odpowiedź" in request.prompt
    assert "pytanie dwa" in request.prompt


def test_the_history_bound_holds_for_a_resumed_turn_too(conn, world):
    """A turn queued behind this one must not reach the prompt, resumed or fresh."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    first_msg, first_job = _turn(conn, cid, world, "c1", "pytanie jeden")
    _turn(conn, cid, world, "c2", "pytanie dwa")

    resumed = _run(conn, first_job, f"{cid}:{first_msg}", resume="session-one")
    fresh = _run(conn, first_job, f"{cid}:{first_msg}", resume=None)

    assert "pytanie dwa" not in resumed.prompt
    assert "pytanie dwa" not in fresh.prompt


def test_the_resume_decision_is_taken_from_the_harness_descriptor():
    """PLC-2, as a shape and not as a word: whether a run may resume is read off the
    registered harness descriptor. A named harness on that path - a vendor string compared
    against `job.agent_type`, a branch per CLI - is the dead shape this bans. Prose about a
    vendor is not, which is why this reads the call graph rather than grepping for a name."""
    tree = ast.parse(Path("src/agento/framework/consumer.py").read_text())
    vendors = {"claude", "codex", "anthropic", "openai", "pi"}
    literals = {n.value.strip('"\'').lower() for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not (literals & vendors), literals & vendors

    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "resume" in attributes
    assert "capabilities" in attributes


# --- Review Focus 2: a deleted view is not a deactivated one (§9) ---------

def test_a_deleted_agent_view_leaves_the_thread_readable_and_refuses_new_turns(
    conn, world
):
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")
    _, job_id = _turn(conn, cid, world, "c1", "pytanie jeden")

    with conn.cursor() as cur:
        cur.execute("DELETE FROM agent_view WHERE id = %s", (world["view"],))
    conn.commit()

    # The in-flight turn survives the delete and still finalizes (Task 12): the job row is
    # kept with a NULL view, so §5.3's finalizer has a row to write its outcome onto.
    with conn.cursor() as cur:
        cur.execute("SELECT agent_view_id FROM job WHERE id = %s", (job_id,))
        assert cur.fetchone() == {"agent_view_id": None}
    conn.commit()

    thread = service.load_visible(conn, conversation_id=cid, user=world["owner"])
    assert thread is not None and thread["agent_view_id"] is None
    assert service.list_messages(conn, conversation_id=cid, limit=50)
    assert routes.messages(_Req(conn, world["owner"], params={"id": cid})).status == 200

    refused = routes.send(_Req(conn, world["owner"], params={"id": cid},
                               json={"client_message_id": "c2", "content": "pytanie dwa"}))
    assert refused.status == 404


def test_a_deactivated_agent_view_hides_the_thread_instead(conn, world):
    """Deactivation is not deletion: the view still exists, so the thread is hidden by
    `scope_is_active()` rather than left readable with a NULL view."""
    cid = service.create_conversation(conn, user_id=world["owner"].id,
                                      agent_view_id=world["view"], title="t")

    with conn.cursor() as cur:
        cur.execute("UPDATE agent_view SET is_active = 0 WHERE id = %s", (world["view"],))
    conn.commit()

    assert service.load_visible(conn, conversation_id=cid, user=world["owner"]) is None
