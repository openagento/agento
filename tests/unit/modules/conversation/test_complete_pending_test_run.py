"""A follow-up turn is a normal job: no publish in `complete_pending` carries `test_config`."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agento.modules.conversation.src import service


@pytest.mark.parametrize("channel", [service.PANEL_CHANNEL, "outlook"])
def test_turn_jobs_are_published_without_a_test_config(channel):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        {"job_id": None, "job_state": "pending", "conversation_id": 1, "id": 2,
         "content": "hi", "agent_view_id": 3, "user_id": 4, "channel": channel,
         "external_ref": "ref", "thread_id": 5},
        {"status": "TODO"},
        {"job_id": 9},
    ]
    cur.rowcount = 0
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with patch.object(service, "publish_job", return_value=9) as publish, \
            patch.object(service, "resolve_job_type", return_value="t"):
        service.complete_pending(conn, 2)
    publish.assert_called_once()
    assert "test_config" not in publish.call_args.kwargs
