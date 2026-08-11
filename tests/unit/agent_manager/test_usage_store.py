from __future__ import annotations

from unittest.mock import MagicMock

from agento.framework.agent_manager.models import UsageSummary
from agento.framework.agent_manager.usage_store import (
    get_usage_summaries,
    get_usage_summary,
    record_usage,
)


def _mock_conn(fetchone_return=None, fetchall_return=None, lastrowid=1):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = fetchall_return or []
    cursor.lastrowid = lastrowid
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestRecordUsage:
    def test_inserts_and_returns_row_id(self):
        conn, cursor = _mock_conn(lastrowid=42)

        row_id = record_usage(
            conn,
            credential_id=1,
            tokens_used=5000,
            input_tokens=3000,
            output_tokens=2000,
            reference_id="AI-123",
            duration_ms=1500,
            model="claude-sonnet-4-20250514",
            harness="claude",
            provider="anthropic",
        )

        assert row_id == 42
        sql = cursor.execute.call_args[0][0]
        assert "INSERT INTO usage_log" in sql
        params = cursor.execute.call_args[0][1]
        assert params == (1, "claude", "anthropic", 5000, 3000, 2000, "AI-123", 1500,
                          "claude-sonnet-4-20250514")

    def test_defaults_reference_and_duration(self):
        conn, cursor = _mock_conn(lastrowid=1)

        record_usage(conn, credential_id=1, tokens_used=100, input_tokens=60, output_tokens=40)

        params = cursor.execute.call_args[0][1]
        assert params == (1, None, None, 100, 60, 40, None, 0, None)


class TestGetUsageSummary:
    def test_returns_summary(self):
        conn, cursor = _mock_conn(fetchone_return={"total_tokens": 50000, "call_count": 10})

        summary = get_usage_summary(conn, credential_id=3, window_hours=12)

        assert isinstance(summary, UsageSummary)
        assert summary.credential_id == 3
        assert summary.total_tokens == 50000
        assert summary.call_count == 10
        params = cursor.execute.call_args[0][1]
        assert params == (3, 12)

    def test_zero_usage(self):
        conn, _cursor = _mock_conn(fetchone_return={"total_tokens": 0, "call_count": 0})

        summary = get_usage_summary(conn, credential_id=1)

        assert summary.total_tokens == 0
        assert summary.call_count == 0


class TestGetUsageSummaries:
    def test_returns_list_of_summaries(self):
        rows = [
            {"credential_id": 1, "total_tokens": 10000, "call_count": 5},
            {"credential_id": 2, "total_tokens": 0, "call_count": 0},
        ]
        conn, cursor = _mock_conn(fetchall_return=rows)

        summaries = get_usage_summaries(conn, scope="claude", window_hours=24)

        assert len(summaries) == 2
        assert summaries[0].credential_id == 1
        assert summaries[0].total_tokens == 10000
        assert summaries[1].total_tokens == 0
        params = cursor.execute.call_args[0][1]
        assert params == (24, "claude")

    def test_empty_result(self):
        conn, _cursor = _mock_conn(fetchall_return=[])

        summaries = get_usage_summaries(conn, scope="codex")

        assert summaries == []
