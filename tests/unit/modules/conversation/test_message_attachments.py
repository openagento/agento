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

from agento.framework import uploads
from agento.modules.conversation.src import service

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


def test_the_turn_publishes_the_users_text_and_its_descriptor():
    conn, _ = fake_conn([row(uploads=json.dumps(DESCRIPTOR)), {"status": "TODO"}, {"job_id": 9}])
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    kwargs = publish.call_args.kwargs
    # The text the user wrote: the framework names the files when the job runs.
    assert kwargs["prompt"] == "hi"
    # The descriptor rides along as the job's own column: the framework reads it, and never
    # the conversation's tables (MOD-1).
    assert json.loads(kwargs["attachments"]) == DESCRIPTOR


def test_a_channel_reply_publishes_no_attachments():
    """Only the panel composer attaches files: a file in a channel reply belongs in the
    external system the reply goes to, which is a different upload entirely (Settled 17)."""
    conn, _ = fake_conn([row(channel="outlook", uploads=json.dumps(DESCRIPTOR)),
                         {"status": "TODO"}, {"job_id": 9}])
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    assert "attachments" not in publish.call_args.kwargs


def test_a_message_that_is_only_files_publishes_no_text():
    conn, _ = fake_conn([row(content="", uploads=json.dumps(DESCRIPTOR)),
                         {"status": "TODO"}, {"job_id": 9}])
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    assert publish.call_args.kwargs["prompt"] == ""


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
                patch.object(service, "uploads_lock"):
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
        with patch.object(service, "uploads_lock"), \
                patch.object(uploads, "descriptor", side_effect=uploads.UploadError(400, "upload not available")), \
                patch.object(service, "complete_pending") as publish, pytest.raises(service.SubmissionError) as caught:
            service.submit_message(conn, conversation_id=1, user_id=4,
                                   client_message_id="c1", content="hi", upload_ids=["ab"])
        assert caught.value.status == 400
        conn.rollback.assert_called()
        publish.assert_not_called()

    def test_a_busy_conversation_is_a_503_not_a_500(self):
        conn, _ = fake_conn([{"id": 2}])
        with patch.object(service, "uploads_lock",
                          side_effect=uploads.UploadError(503, "uploads are busy, try again")), \
                pytest.raises(service.SubmissionError) as caught:
            service.submit_message(conn, conversation_id=1, user_id=4,
                                   client_message_id="c1", content="hi", upload_ids=["ab"])
        assert caught.value.status == 503

    def test_a_message_of_only_files_is_titled_by_its_first_file(self):
        with patch.object(uploads, "descriptor", return_value=DESCRIPTOR):
            conn, cur = fake_conn([{"id": 2}])
            cur.lastrowid = 2
            with patch.object(service, "complete_pending", return_value=9), \
                    patch.object(service, "uploads_lock"):
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
    def sweep(self, rows, expired=("ab",), delivered=False):
        conn, _ = fake_conn([])
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = rows
        with patch.object(service, "uploads_lock"), \
                patch.object(service, "attachments_delivered", return_value=delivered), \
                patch.object(uploads, "owners", return_value=[1]), \
                patch.object(uploads, "users", return_value=[4]), \
                patch.object(uploads, "expired_dirs", return_value=list(expired)), \
                patch.object(uploads, "remove") as remove:
            taken = service.sweep_uploads(conn, retention_hours=24)
        return taken, remove

    def row(self, **over):
        return {"uploads": json.dumps(DESCRIPTOR), "thread_id": 5, "thread_key": "k" * 32, **over}

    def test_an_expired_file_no_live_message_names_is_taken(self):
        taken, remove = self.sweep([])
        assert taken == 1
        remove.assert_called_once_with("t1", 4, "ab")

    def test_a_file_whose_copy_the_thread_does_not_hold_yet_is_kept(self):
        """Delivery copies from staging at run preparation, and a retry prepares again."""
        taken, remove = self.sweep([self.row()], delivered=False)
        assert taken == 0
        remove.assert_not_called()

    def test_a_confirmed_delivery_releases_its_source_at_once(self):
        """The thread holds the copy: nothing will ever read the staged one again."""
        taken, remove = self.sweep([self.row()], delivered=True)
        assert taken == 1
        remove.assert_called_once_with("t1", 4, "ab")

    def test_a_terminal_turn_that_never_delivered_still_keeps_its_source(self):
        """A run can fail DURING preparation, before the copy. Job state is not the signal:
        a Retry of that turn must still find the only copy there (BUG)."""
        taken, remove = self.sweep([self.row(job_state="terminal")], delivered=False)
        assert taken == 0
        remove.assert_not_called()

    def test_a_busy_conversation_does_not_end_the_tick(self):
        conn, _ = fake_conn([])
        with patch.object(service, "uploads_lock", side_effect=uploads.UploadError(503, "busy")), \
                patch.object(uploads, "owners", return_value=[1, 2]), \
                patch.object(uploads, "remove") as remove:
            assert service.sweep_uploads(conn, retention_hours=24) == 0
        remove.assert_not_called()


class TestTheRebuiltHistory:
    """The run is NOT given the prompt the publish wrote: `build_prompt` rebuilds the history
    from the `message` rows. An EARLIER turn's files travel in that rebuild; the files of the
    turn this job runs for are named once, by the framework (`Workflow.execute`) (AG-87)."""

    @pytest.mark.parametrize(("turn", "text"), [
        ({"content": "podsumuj plik", "uploads": json.dumps(DESCRIPTOR)},
         "podsumuj plik\n\nAttached files:\n- ./shared/attachments/m2/report.pdf"),
        ({"content": "", "uploads": DESCRIPTOR},
         "Attached files:\n- ./shared/attachments/m2/report.pdf"),
        ({"content": "hi", "uploads": None}, "hi"),
    ])
    def test_an_earlier_turn_carries_its_files(self, turn, text):
        from agento.modules.conversation.src import workflow

        assert workflow.turn_text(turn) == text

    @staticmethod
    def _flow(turns):
        from agento.modules.conversation.src import workflow

        cur = MagicMock()
        cur.fetchall.return_value = turns
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        channel = MagicMock()
        channel.get_prompt_fragments.return_value = MagicMock(read_context="ctx", respond="odpowiedz")
        runner = MagicMock()
        runner.execute.return_value = MagicMock()
        return workflow.ConversationWorkflow(runner=runner, logger=MagicMock()), channel, conn, runner

    def test_the_run_names_this_turns_files_once(self):
        from agento.framework.job_models import AgentType, Job
        from agento.framework.workflows.base import JobContext

        earlier = {"key": "m1", "files": [{"from": "uploads/t1/u4/cd/old.txt", "as": "old.txt"}]}
        flow, channel, conn, runner = self._flow([
            {"role": "user", "content": "najpierw to", "uploads": json.dumps(earlier)},
            {"role": "assistant", "content": "ok", "uploads": None},
            {"role": "user", "content": "podsumuj plik", "uploads": json.dumps(DESCRIPTOR)},
        ])
        context = JobContext(config={}, logger=MagicMock(), update_reference_id=lambda *a: None,
                             attachments=json.dumps(DESCRIPTOR))
        with patch("agento.modules.conversation.src.workflow.get_connection", return_value=conn):
            flow.execute_job(channel, Job.stub(type=AgentType.BLANK, source="conversation", reference_id="1:2"), context)
        prompt = runner.execute.call_args.args[0].prompt
        assert prompt.count("./shared/attachments/m2/report.pdf") == 1
        assert prompt.endswith("Attached files:\n- ./shared/attachments/m2/report.pdf")
        assert "./shared/attachments/m1/old.txt" in prompt
        # The query has to ask for the column at all - that is where the paths were lost.
        assert "uploads" in conn.cursor.return_value.__enter__.return_value.execute.call_args[0][0]


class TestDescriptorJson:
    @pytest.mark.parametrize("value", [json.dumps(DESCRIPTOR), json.dumps(DESCRIPTOR).encode(), DESCRIPTOR])
    def test_every_shape_the_driver_returns_is_the_same_text(self, value):
        assert json.loads(uploads.descriptor_json(value)) == DESCRIPTOR

    def test_no_value_is_none(self):
        assert uploads.descriptor_json(None) is None
