from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.job_models import AgentType, JobRequester, RequesterTrust
from agento.framework.publisher import publish


def _mock_connection(rowcount=1):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = rowcount
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


@patch("agento.framework.publisher.get_connection")
def test_publish_inserts_job(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection(rowcount=1)
    mock_cursor.fetchone.return_value = None  # dedupe SELECT: no existing row
    mock_get_conn.return_value = mock_conn

    result = publish(sample_config, AgentType.CRON, "jira", "key:1", reference_id="AI-1")

    assert result is True
    sql_calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
    assert any("SELECT id FROM job WHERE idempotency_key" in s for s in sql_calls)
    insert_sql = next(s for s in sql_calls if "INSERT" in s)
    assert "INSERT INTO job" in insert_sql
    assert "INSERT IGNORE" not in insert_sql


@patch("agento.framework.publisher.get_connection")
def test_publish_duplicate_returns_false(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_cursor.fetchone.return_value = {"id": 7}  # dedupe SELECT finds an existing row
    mock_get_conn.return_value = mock_conn

    result = publish(sample_config, AgentType.CRON, "jira", "key:dup")

    assert result is False
    sql_calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
    assert not any("INSERT" in s for s in sql_calls)


@patch("agento.framework.publisher.get_connection")
def test_publish_commits_on_success(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection(rowcount=1)
    mock_cursor.fetchone.return_value = None
    mock_get_conn.return_value = mock_conn

    publish(sample_config, AgentType.CRON, "jira", "key:commit")

    mock_conn.commit.assert_called_once()


@patch("agento.framework.publisher.get_connection")
def test_publish_race_returns_false(mock_get_conn, sample_config):
    """Two publishers slip past the dedupe SELECT; the unique key rejects the
    second INSERT. The IntegrityError is swallowed and reported as a duplicate."""
    import pymysql

    mock_conn, mock_cursor = _mock_connection(rowcount=1)
    mock_cursor.fetchone.return_value = None  # SELECT sees no row
    mock_cursor.execute.side_effect = [
        None,  # dedupe SELECT
        pymysql.err.IntegrityError(1062, "Duplicate entry"),  # INSERT loses the race
    ]
    mock_get_conn.return_value = mock_conn

    result = publish(sample_config, AgentType.CRON, "jira", "key:race")

    assert result is False
    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()


@patch("agento.framework.publisher.get_connection")
def test_publish_rollback_on_error(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_cursor.execute.side_effect = RuntimeError("DB down")
    mock_get_conn.return_value = mock_conn

    with pytest.raises(RuntimeError, match="DB down"):
        publish(sample_config, AgentType.CRON, "jira", "key:fail")

    mock_conn.rollback.assert_called_once()


@patch("agento.framework.publisher.get_connection")
def test_publish_closes_connection(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection(rowcount=1)
    mock_cursor.fetchone.return_value = None
    mock_get_conn.return_value = mock_conn

    publish(sample_config, AgentType.CRON, "jira", "key:close")
    mock_conn.close.assert_called_once()

    # Also verify close on failure path
    mock_conn.reset_mock()
    mock_cursor.execute.side_effect = RuntimeError("fail")
    mock_get_conn.return_value = mock_conn

    with pytest.raises(RuntimeError):
        publish(sample_config, AgentType.CRON, "jira", "key:close2")

    mock_conn.close.assert_called_once()


class TestSkipIfActive:
    """Guard: when skip_if_active=True and reference_id is set, block publish
    if a non-terminal job already exists for (type, source, agent_view_id,
    reference_id). Prevents duplicate enqueues from Jira index lag and
    similar races when the idempotency key rotates on every remote update.
    """

    @patch("agento.framework.publisher.get_connection")
    def test_blocks_when_active_job_exists(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection()
        mock_cursor.fetchone.return_value = (1,)
        mock_get_conn.return_value = mock_conn

        result = publish(
            sample_config, AgentType.TODO, "jira", "key:new",
            reference_id="AI-64", agent_view_id=2, skip_if_active=True,
        )

        assert result is False
        sql_calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert any("SELECT" in sql for sql in sql_calls)
        assert not any("INSERT" in sql for sql in sql_calls)

    @patch("agento.framework.publisher.get_connection")
    def test_inserts_when_no_active_job(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        result = publish(
            sample_config, AgentType.TODO, "jira", "key:new",
            reference_id="AI-64", agent_view_id=2, skip_if_active=True,
        )

        assert result is True
        sql_calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        assert any("SELECT" in sql for sql in sql_calls)
        assert any("INSERT INTO job" in sql for sql in sql_calls)
        assert not any("INSERT IGNORE" in sql for sql in sql_calls)

    @patch("agento.framework.publisher.get_connection")
    def test_no_precheck_without_reference_id(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        result = publish(
            sample_config, AgentType.TODO, "jira", "key:x",
            reference_id=None, skip_if_active=True,
        )

        assert result is True
        sql_calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        # No skip_if_active precheck (its SELECT filters on status), but the
        # idempotency dedupe SELECT still runs before the INSERT.
        assert not any("status IN" in sql for sql in sql_calls)
        assert any("idempotency_key" in sql and "SELECT" in sql for sql in sql_calls)

    @patch("agento.framework.publisher.get_connection")
    def test_disabled_by_default(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        publish(sample_config, AgentType.TODO, "jira", "key:d", reference_id="AI-1")

        sql_calls = [c[0][0] for c in mock_cursor.execute.call_args_list]
        # skip_if_active defaults off, so no status-filtered precheck SELECT.
        assert not any("status IN" in sql for sql in sql_calls)

    @patch("agento.framework.publisher.get_connection")
    def test_precheck_filters_by_type_source_view_and_reference(
        self, mock_get_conn, sample_config
    ):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        publish(
            sample_config, AgentType.TODO, "jira", "key:p",
            reference_id="AI-64", agent_view_id=2, skip_if_active=True,
        )

        select_call = next(
            c for c in mock_cursor.execute.call_args_list
            if "SELECT" in c[0][0]
        )
        sql, params = select_call[0][0], select_call[0][1]
        assert "type" in sql and "source" in sql
        assert "agent_view_id" in sql and "reference_id" in sql
        assert "status" in sql
        assert params == ("todo", "jira", 2, "AI-64")


def _insert_call(mock_cursor):
    """Return the (sql, params) of the INSERT execute call."""
    return next(
        (c[0][0], c[0][1]) for c in mock_cursor.execute.call_args_list
        if "INSERT INTO job" in c[0][0]
    )


class TestRequester:
    """requester is threaded into the INSERT (audit metadata only - never dedupe)."""

    @patch("agento.framework.publisher.get_connection")
    def test_no_requester_inserts_null_claimed_defaults(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        publish(sample_config, AgentType.CRON, "jira", "key:nr", reference_id="AI-1")

        sql, params = _insert_call(mock_cursor)
        for col in ("requester_key", "requester_email", "requester_trust", "requester_meta"):
            assert col in sql
        # last 4 params are the requester fields
        assert params[-4:] == (None, None, "claimed", None)

    @patch("agento.framework.publisher.get_connection")
    def test_requester_values_persisted(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        requester = JobRequester(
            key="jira:acct-1", email="USER@Example.com",
            trust=RequesterTrust.ACCOUNT, meta={"basis": "comment_author"},
        )
        publish(
            sample_config, AgentType.TODO, "jira", "key:r",
            reference_id="AI-2", requester=requester,
        )

        _sql, params = _insert_call(mock_cursor)
        r_key, r_email, r_trust, r_meta = params[-4:]
        assert r_key == "jira:acct-1"
        assert r_email == "user@example.com"  # normalized
        assert r_trust == "account"
        assert json.loads(r_meta) == {"basis": "comment_author"}

    @patch("agento.framework.publisher.get_connection")
    def test_requester_does_not_change_idempotency_key_param(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        requester = JobRequester(key="jira:x", trust=RequesterTrust.ACCOUNT)
        publish(
            sample_config, AgentType.TODO, "jira", "idem:abc",
            reference_id="AI-3", requester=requester,
        )

        _sql, params = _insert_call(mock_cursor)
        assert "idem:abc" in params  # idempotency_key passed through unchanged

    @patch("agento.framework.publisher.get_connection")
    def test_requester_does_not_affect_skip_if_active_select(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn

        requester = JobRequester(key="jira:x", trust=RequesterTrust.ACCOUNT)
        publish(
            sample_config, AgentType.TODO, "jira", "key:s",
            reference_id="AI-64", agent_view_id=2, skip_if_active=True,
            requester=requester,
        )

        select_call = next(
            c for c in mock_cursor.execute.call_args_list if "SELECT" in c[0][0]
        )
        # SELECT params unchanged by the requester - still only the 4 dedupe keys
        assert select_call[0][1] == ("todo", "jira", 2, "AI-64")

    @patch("agento.framework.publisher.get_event_manager")
    @patch("agento.framework.publisher.get_connection")
    def test_published_event_carries_requester(self, mock_get_conn, mock_get_em, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn
        em = MagicMock()
        mock_get_em.return_value = em

        requester = JobRequester(key="jira:x", trust=RequesterTrust.ACCOUNT)
        publish(
            sample_config, AgentType.TODO, "jira", "key:e",
            reference_id="AI-5", requester=requester,
        )

        em.dispatch.assert_called_once()
        name, event = em.dispatch.call_args[0]
        assert name == "job_publish_after"
        assert event.requester is requester
