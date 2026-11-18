"""The composer's files, end to end through the routes (AG-87).

Staging, the claim and the delivery descriptor, against a real database: the route stages
one file, the submission claims it, and the message that comes back names the paths the run
will see. The gates are the ones `send` has - a caller who cannot post cannot stage either.
"""
from __future__ import annotations

import json

import pytest

from agento.modules.conversation.src import routes, service, uploads

from .conftest import _clean  # noqa: F401
from .test_conversation_submission import _Req


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOADS_DIR", str(tmp_path / "uploads"))
    return tmp_path


@pytest.fixture
def thread(conn, world):
    return service.create_conversation(conn, user_id=world["owner"].id,
                                       agent_view_id=world["view"], title="t")


class _Upload(_Req):
    """A multipart POST: `web` hands the handler the raw body and the headers."""

    def __init__(self, conn, user, thread_id, *, filename="report.pdf", payload=b"bytes",
                 content_type=None):
        super().__init__(conn, user, params={"id": thread_id})
        boundary = "B"
        self.body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\n\r\n".encode() + payload
            + f"\r\n--{boundary}--\r\n".encode())
        self.headers = {"Content-Type": content_type
                        or f"multipart/form-data; boundary={boundary}"}


def test_a_staged_file_becomes_the_messages_attachment(conn, world, thread, workspace):
    staged = routes.upload(_Upload(conn, world["owner"], thread))
    assert staged.status == 201
    assert staged.body["name"] == "report.pdf" and staged.body["bytes"] == 5

    sent = routes.send(_Req(conn, world["owner"], params={"id": thread},
                            json={"content": "look", "client_message_id": "c1",
                                  "upload_ids": [staged.body["upload_id"]]}))
    assert sent.status == 201
    with conn.cursor() as cur:
        cur.execute("SELECT uploads FROM message WHERE id = %s", (sent.body["message_id"],))
        descriptor = json.loads(cur.fetchone()["uploads"])
        cur.execute("SELECT prompt, attachments FROM job WHERE id = %s", (sent.body["job_id"],))
        job = cur.fetchone()
    conn.commit()
    assert descriptor["files"][0]["as"] == "report.pdf"
    assert json.loads(job["attachments"]) == descriptor
    # The user's text: the framework names the files when the job runs (`Workflow.execute`).
    assert job["prompt"] == "look"


def test_a_message_of_only_files_is_a_turn_of_its_own(conn, world, thread, workspace):
    staged = routes.upload(_Upload(conn, world["owner"], thread, filename="notes.txt"))
    sent = routes.send(_Req(conn, world["owner"], params={"id": thread},
                            json={"content": "", "client_message_id": "c1",
                                  "upload_ids": [staged.body["upload_id"]]}))
    assert sent.status == 201


def test_a_submission_with_no_text_and_no_file_is_still_refused(conn, world, thread, workspace):
    refused = routes.send(_Req(conn, world["owner"], params={"id": thread},
                               json={"content": "", "client_message_id": "c1"}))
    assert refused.status == 400


def test_another_users_id_is_not_usable(conn, world, thread, workspace):
    staged = routes.upload(_Upload(conn, world["owner"], thread))
    refused = routes.send(_Req(conn, world["stranger"], params={"id": thread},
                               json={"content": "mine now", "client_message_id": "c2",
                                     "upload_ids": [staged.body["upload_id"]]}))
    assert refused.status in (400, 403, 404)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM message WHERE content = 'mine now'")
        assert cur.fetchone()["n"] == 0
    conn.commit()


def test_a_regeneration_keeps_the_same_delivered_files(conn, world, thread, workspace):
    staged = routes.upload(_Upload(conn, world["owner"], thread))
    sent = routes.send(_Req(conn, world["owner"], params={"id": thread},
                            json={"content": "look", "client_message_id": "c1",
                                  "upload_ids": [staged.body["upload_id"]]}))
    again = routes.regenerate(_Req(conn, world["owner"], params={"id": thread},
                                   json={"message_id": sent.body["message_id"],
                                         "client_message_id": "c2"}))
    assert again.status == 201
    with conn.cursor() as cur:
        cur.execute("SELECT uploads FROM message WHERE id IN (%s, %s)",
                    (sent.body["message_id"], again.body["message_id"]))
        both = [json.loads(r["uploads"]) for r in cur.fetchall()]
    conn.commit()
    assert both[0] == both[1]           # key included: the thread's copy is reused


def test_a_body_that_is_not_multipart_is_refused(conn, world, thread, workspace):
    refused = routes.upload(_Upload(conn, world["owner"], thread, content_type="application/json"))
    assert refused.status == 400


def test_a_file_over_the_limit_is_refused(conn, world, thread, workspace):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value) "
                    "VALUES ('default', 0, 'conversation/limits/upload_max_bytes', '1024')")
    conn.commit()
    refused = routes.upload(_Upload(conn, world["owner"], thread, payload=b"x" * 2048))
    assert refused.status == 413


def test_a_thread_the_caller_cannot_reach_stages_nothing(conn, world, workspace):
    other = service.create_conversation(conn, user_id=world["stranger"].id,
                                        agent_view_id=world["view"], title="theirs")
    refused = routes.upload(_Upload(conn, world["owner"], other))
    assert refused.status == 404
    assert uploads.conversations_with_uploads() == []


def test_a_regeneration_that_also_carries_upload_ids_is_refused(conn, world, thread, workspace):
    """The two are alternatives, not a merge: a silent drop would send a message the user
    believes carries the file they just picked (B-7)."""
    staged = routes.upload(_Upload(conn, world["owner"], thread))
    sent = routes.send(_Req(conn, world["owner"], params={"id": thread},
                            json={"content": "look", "client_message_id": "c1",
                                  "upload_ids": [staged.body["upload_id"]]}))
    refused = routes.regenerate(_Req(conn, world["owner"], params={"id": thread},
                                     json={"message_id": sent.body["message_id"],
                                           "client_message_id": "c2", "upload_ids": []}))
    assert refused.status == 400
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s "
                    "AND role = 'user'", (thread,))
        assert cur.fetchone()["n"] == 1            # nothing was re-asked
    conn.commit()


def test_the_sweep_runs_its_query_against_a_real_database(conn, world, thread, workspace):
    """The sweep's SELECT joins message -> conversation -> thread. A mocked cursor accepts
    any SQL; only a real database says whether the columns are there."""
    staged = routes.upload(_Upload(conn, world["owner"], thread))
    routes.send(_Req(conn, world["owner"], params={"id": thread},
                     json={"content": "look", "client_message_id": "c1",
                           "upload_ids": [staged.body["upload_id"]]}))
    # Nothing has expired, so the sweep takes nothing - it must still complete.
    assert service.sweep_uploads(conn, retention_hours=24) == 0


def _channel(conn, world):
    """A conversation that mirrors an external system."""
    conversation = service.create_conversation(conn, user_id=world["owner"].id,
                                               agent_view_id=world["view"], title="mail")
    with conn.cursor() as cur:
        cur.execute("UPDATE conversation SET channel = 'outlook', external_ref = 'r1' "
                    "WHERE id = %s", (conversation,))
    conn.commit()
    return conversation


def test_a_channel_thread_stages_no_file(conn, world, workspace):
    """A channel turn is published with no attachment descriptor (B-6), so a file accepted
    here would reach the agent as a path to something that was never delivered."""
    # An admin: the channel's own write gate must not be what refuses this.
    refused = routes.upload(_Upload(conn, world["admin"], _channel(conn, world)))
    assert refused.status == 400
    assert uploads.conversations_with_uploads() == []


def test_a_channel_thread_refuses_upload_ids(conn, world, workspace):
    channel = _channel(conn, world)
    # Staged INTO the channel itself: a file staged elsewhere is already refused for
    # belonging to another conversation, which would hide the removal of this gate.
    upload_id, _ = uploads.store(channel, world["admin"].id, "report.pdf", b"bytes")
    refused = routes.send(_Req(conn, world["admin"], params={"id": channel},
                               json={"content": "look", "client_message_id": "c1",
                                     "upload_ids": [upload_id]}))
    assert refused.status == 400
    assert refused.body["error"] == "this conversation takes no files"
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM message WHERE conversation_id = %s", (channel,))
        assert cur.fetchone()["n"] == 0
    conn.commit()
