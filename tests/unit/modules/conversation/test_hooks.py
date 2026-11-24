"""The execution row carries the run's tool risk assessment (Task 6)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agento.framework.execution_hooks import ExecutionIdProvider, RunProfile
from agento.modules.conversation.src.hooks import ConversationExecutionIds


def _conn():
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


def _mint(profile):
    conn, cur = _conn()
    with patch("agento.modules.conversation.src.hooks.service.link_execution"):
        ConversationExecutionIds().mint(conn=conn, job_id=7, attempt=1, profile=profile)
    return cur.execute.call_args_list[0].args


def test_mint_writes_tool_risk():
    risk = {"v": 1, "trifecta": "possible", "direct": "pending"}
    sql, params = _mint(RunProfile(harness="h", provider="p", model=None, credential_id=None,
                                   tool_risk=risk))
    assert "tool_risk" in sql
    assert json.loads(params[-1]) == risk


def test_mint_without_assessment_writes_null():
    _, params = _mint(RunProfile(harness="h", provider="p", model=None, credential_id=None))
    assert params[-1] is None
    _, params = _mint(None)
    assert params[-1] is None


def test_record_tool_risk_is_one_update_by_execution_id():
    conn, cur = _conn()
    ConversationExecutionIds().record_tool_risk(conn=conn, execution_id="e-1", tool_risk={"v": 1})
    assert cur.execute.call_count == 1
    sql, params = cur.execute.call_args.args
    assert sql == "UPDATE execution SET tool_risk = %s WHERE execution_id = %s"
    assert params == ('{"v": 1}', "e-1")
    conn.commit.assert_not_called()


def test_provider_still_satisfies_the_protocol():
    assert isinstance(ConversationExecutionIds(), ExecutionIdProvider)
