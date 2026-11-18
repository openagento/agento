"""A message carries its files from the composer to the run (AG-87).

The claim is a pure DB write: the descriptor is written beside the message, in the message's
own transaction, and nothing on disk is moved. Everything after that reads `message.uploads`
- the publish, the regeneration and the sweep - so these tests fake the connection and
assert what each of them does with that one column.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.conversation.src import service, uploads

DESCRIPTOR = {"key": "m2", "files": [{"from": "uploads/t1/u4/ab/report.pdf", "as": "report.pdf"}]}


def row(**over):
    base = {"job_id": None, "job_state": "pending", "conversation_id": 1, "id": 2,
            "content": "hi", "agent_view_id": 3, "author_user_id": 4,
            "channel": service.PANEL_CHANNEL, "external_ref": "ref", "thread_id": 5,
            "uploads": None}
    return {**base, **over}


def fake_conn(fetches, rowcount=0):
    cur = MagicMock()
    cur.fetchone.side_effect = fetches
    cur.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


@pytest.mark.parametrize("channel,field", [(service.PANEL_CHANNEL, "prompt"), ("outlook", "context")])
def test_the_turn_tells_the_agent_where_its_files_are(channel, field):
    conn, _ = fake_conn([row(channel=channel, uploads=json.dumps(DESCRIPTOR)),
                         {"status": "TODO"}, {"job_id": 9}])
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    kwargs = publish.call_args.kwargs
    assert kwargs[field] == "hi\n\nAttached files:\n- ./shared/attachments/m2/report.pdf"
    # The descriptor rides along as the job's own column: the framework reads it, and never
    # the conversation's tables (MOD-1).
    assert json.loads(kwargs["attachments"]) == DESCRIPTOR


def test_a_message_that_is_only_files_is_prompted_with_the_block_alone():
    conn, _ = fake_conn([row(content="", uploads=json.dumps(DESCRIPTOR)),
                         {"status": "TODO"}, {"job_id": 9}])
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    assert publish.call_args.kwargs["prompt"] == "Attached files:\n- ./shared/attachments/m2/report.pdf"


def test_a_turn_with_no_files_publishes_none():
    conn, _ = fake_conn([row(), {"status": "TODO"}, {"job_id": 9}])
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    assert publish.call_args.kwargs["attachments"] is None
    assert publish.call_args.kwargs["prompt"] == "hi"


class TestClaim:
    """`submit_message` writes the descriptor inside the message's own transaction."""

    def run(self, cur_fetch=None, **kwargs):
        conn, cur = fake_conn(cur_fetch or [{"id": 2}])
        cur.lastrowid = 2
        with patch.object(service, "complete_pending", return_value=9), \
                patch.object(uploads, "uploads_lock"):
            service.submit_message(conn, conversation_id=1, user_id=4,
                                   client_message_id="c1", content="hi", **kwargs)
        return cur

    def test_the_descriptor_is_written_beside_the_message(self):
        with patch.object(uploads, "descriptor", return_value=DESCRIPTOR):
            cur = self.run(upload_ids=["ab"])
        written = [c for c in cur.execute.call_args_list if "message SET uploads" in c.args[0]]
        assert len(written) == 1
        assert json.loads(written[0].args[1][0]) == DESCRIPTOR

    def test_a_file_the_caller_may_not_use_refuses_the_whole_message(self):
        """No message, no job: a submission is its files or it is nothing."""
        conn, cur = fake_conn([{"id": 2}])
        cur.lastrowid = 2
        with patch.object(uploads, "uploads_lock"), \
                patch.object(uploads, "descriptor", side_effect=uploads.UploadError(400, "upload not available")), \
                patch.object(service, "complete_pending") as publish:
            with pytest.raises(service.SubmissionError) as caught:
                service.submit_message(conn, conversation_id=1, user_id=4,
                                       client_message_id="c1", content="hi", upload_ids=["ab"])
        assert caught.value.status == 400
        conn.rollback.assert_called()
        publish.assert_not_called()

    def test_a_busy_conversation_is_a_503_not_a_500(self):
        conn, cur = fake_conn([{"id": 2}])
        with patch.object(uploads, "uploads_lock",
                          side_effect=uploads.UploadError(503, "uploads are busy, try again")):
            with pytest.raises(service.SubmissionError) as caught:
                service.submit_message(conn, conversation_id=1, user_id=4,
                                       client_message_id="c1", content="hi", upload_ids=["ab"])
        assert caught.value.status == 503

    def test_a_message_of_only_files_is_titled_by_its_first_file(self):
        with patch.object(uploads, "descriptor", return_value=DESCRIPTOR):
            conn, cur = fake_conn([{"id": 2}])
            cur.lastrowid = 2
            with patch.object(service, "complete_pending", return_value=9), \
                    patch.object(uploads, "uploads_lock"):
                service.submit_message(conn, conversation_id=1, user_id=4,
                                       client_message_id="c1", content="", upload_ids=["ab"])
        titles = [c for c in cur.execute.call_args_list if "SET title" in c.args[0]]
        assert titles[0].args[1][0] == "report.pdf"


def test_a_regeneration_reuses_the_files_the_original_already_delivered():
    """Verbatim, `key` included: the thread's copy is reused and nothing is uploaded again -
    which is the only thing that can work, since staging is swept long before."""
    conn, _ = fake_conn([{"content": "hi", "uploads": json.dumps(DESCRIPTOR)}])
    with patch.object(service, "submit_message", return_value=(2, 9, True)) as submit:
        service.regenerate(conn, conversation_id=1, message_id=2, user_id=4, client_message_id="c2")
    assert json.loads(submit.call_args.kwargs["uploads_json"]) == DESCRIPTOR


class TestSweep:
    def sweep(self, rows, expired=("ab",)):
        conn, _ = fake_conn([])
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
        with patch.object(uploads, "uploads_lock"), \
                patch.object(uploads, "conversations_with_uploads", return_value=[1]), \
                patch.object(uploads, "users_with_uploads", return_value=[4]), \
                patch.object(uploads, "expired_dirs", return_value=list(expired)), \
                patch.object(uploads, "remove") as remove:
            taken = service.sweep_uploads(conn, retention_hours=24)
        return taken, remove

    def test_an_expired_file_no_live_message_names_is_taken(self):
        taken, remove = self.sweep([])
        assert taken == 1
        remove.assert_called_once_with(1, 4, "ab")

    def test_a_file_a_live_message_still_needs_is_kept(self):
        """Delivery copies from staging at run preparation, and a retry prepares again."""
        taken, remove = self.sweep([{"uploads": json.dumps(DESCRIPTOR)}])
        assert taken == 0
        remove.assert_not_called()

    def test_a_busy_conversation_does_not_end_the_tick(self):
        conn, _ = fake_conn([])
        with patch.object(uploads, "uploads_lock", side_effect=uploads.UploadError(503, "busy")), \
                patch.object(uploads, "conversations_with_uploads", return_value=[1, 2]), \
                patch.object(uploads, "remove") as remove:
            assert service.sweep_uploads(conn, retention_hours=24) == 0
        remove.assert_not_called()
