"""The Outlook thread gate: a mail-conversation thread holds only while thread read is on."""
from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.events import JobClaimBeforeEvent
from agento.modules.outlook.src import thread_gate
from agento.modules.outlook.src.thread_gate import OutlookThreadGate, conversation_ref

G = "agento.modules.outlook.src.thread_gate"


def _claim(thread_row, *, read_on=True):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = thread_row
    event = JobClaimBeforeEvent(job_id=10)
    with (patch(f"{G}.pooled", return_value=nullcontext(conn)),
          patch(f"{G}.DatabaseConfig"),
          patch(f"{G}.thread_read_on", return_value=read_on) as gate):
        OutlookThreadGate().execute(event)
    return event, gate


def test_the_conversation_ref_has_its_own_prefix():
    # Never a job's default ref (`outlook:<reference_id>`), which needs no gate.
    assert conversation_ref("AAQk==") == "outlook:conversation:AAQk=="


@pytest.mark.parametrize(("read_on", "detached"), [(True, False), (False, True)])
def test_a_conversation_thread_detaches_when_thread_read_is_off_for_the_threads_view(read_on, detached):
    event, gate = _claim({"ref": conversation_ref("c1"), "agent_view_id": 3}, read_on=read_on)

    assert event.detach_thread is detached
    gate.assert_called_once()
    assert gate.call_args.args[1] == 3                 # the THREAD's view, not the job's


def test_a_conversation_thread_with_no_view_detaches():
    event, gate = _claim({"ref": conversation_ref("c1"), "agent_view_id": None})

    assert event.detach_thread is True
    gate.assert_not_called()


@pytest.mark.parametrize("row", [
    None,                                              # no thread yet (the claim places it)
    {"ref": "outlook:re-x::m1", "agent_view_id": 3},   # a message's own default thread
    {"ref": "jira:AI-1", "agent_view_id": 3},          # another channel
])
def test_any_other_thread_is_left_alone(row):
    event, gate = _claim(row, read_on=False)

    assert event.detach_thread is False
    gate.assert_not_called()


def test_an_unreadable_gate_raises_so_the_claim_defers_the_job():
    """`job_claim_before` is not fail-open: an observer that raises defers the claim, so a
    config read that fails never lets the job resume the conversation's session."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {
        "ref": conversation_ref("c1"), "agent_view_id": 3}
    with (patch(f"{G}.pooled", return_value=nullcontext(conn)), patch(f"{G}.DatabaseConfig"),
          patch(f"{G}.thread_read_on", side_effect=RuntimeError("query failed")),
          pytest.raises(RuntimeError)):
        OutlookThreadGate().execute(JobClaimBeforeEvent(job_id=10))


def test_thread_read_is_the_outlook_list_thread_tool():
    with patch(f"{G}.is_effective_for_view", return_value=True) as effective:
        assert thread_gate.thread_read_on("conn", 5) is True
    effective.assert_called_once_with("conn", "outlook_list_thread", agent_view_id=5)
