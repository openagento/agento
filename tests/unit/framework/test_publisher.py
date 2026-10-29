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
    insert_sql = next(s for s in sql_calls if "INSERT INTO job" in s)
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
        assert params == ("todo", "jira", 2, "AI-64", ("TODO", "RUNNING", "PAUSED"))


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
        # the requester fields come before harness, model, the four thread columns and dry_run
        assert params[-11:-7] == (None, None, "claimed", None)

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
        r_key, r_email, r_trust, r_meta = params[-11:-7]
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
        assert select_call[0][1] == ("todo", "jira", 2, "AI-64", ("TODO", "RUNNING", "PAUSED"))

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


@patch("agento.framework.publisher.get_connection")
def test_publish_pins_harness_and_model(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection(rowcount=1)
    mock_cursor.fetchone.return_value = None
    mock_get_conn.return_value = mock_conn

    publish(
        sample_config, AgentType.TODO, "jira", "key:pin",
        harness="codex", model="gpt-x",
    )

    insert_sql, params = next(
        c[0] for c in mock_cursor.execute.call_args_list if "INSERT INTO job" in c[0][0]
    )
    assert "agent_type" in insert_sql and "model" in insert_sql
    assert params[-7:-5] == ("codex", "gpt-x")


@patch("agento.framework.publisher.get_connection")
def test_publish_without_pins_writes_nulls(mock_get_conn, sample_config):
    mock_conn, mock_cursor = _mock_connection(rowcount=1)
    mock_cursor.fetchone.return_value = None
    mock_get_conn.return_value = mock_conn

    publish(sample_config, AgentType.TODO, "jira", "key:nopin")

    params = next(
        c[0][1] for c in mock_cursor.execute.call_args_list if "INSERT" in c[0][0]
    )
    assert params[-7:-5] == (None, None)


@patch("agento.framework.publisher.get_connection")
def test_pins_are_not_part_of_the_idempotency_key(mock_get_conn, sample_config):
    """A second publish of the same key is a duplicate whatever the pins are."""
    mock_conn, mock_cursor = _mock_connection()
    mock_cursor.fetchone.return_value = {"id": 7}
    mock_get_conn.return_value = mock_conn

    assert publish(
        sample_config, AgentType.TODO, "jira", "key:same", harness="codex",
    ) is False


class TestInsertJobThread:
    """Every job gets its thread in the insert (framework/threads.py)."""

    _COLUMNS = ("parent_job_id", "depth", "thread_id", "line_id")

    def _insert(self, **kwargs):
        from agento.framework.publisher import insert_job

        cur = MagicMock()
        cur.lastrowid = 41
        ensure = MagicMock(return_value=6)
        with (patch("agento.framework.publisher.ensure_thread", ensure),
              patch("agento.framework.publisher.join_thread") as self.joined):
            insert_job(cur, agent_type=AgentType.TODO, source="jira", agent_view_id=3,
                       priority=50, idempotency_key="k", max_attempts=3, requester=None,
                       **{"reference_id": "AI-1", **kwargs})
        sql, params = next(c.args for c in cur.execute.call_args_list if "INSERT INTO job" in c.args[0])
        assert "parent_job_id, depth, thread_id, line_id, dry_run)" in sql
        self.dry_run = params[-1]
        return ensure, cur, dict(zip(self._COLUMNS, params[-5:-1], strict=True))

    @pytest.mark.parametrize(("kwargs", "stored"), [({}, 0), ({"dry_run": True}, 1)])
    def test_dry_run_is_written_in_the_insert(self, kwargs, stored):
        self._insert(**kwargs)
        assert self.dry_run == stored

    @pytest.mark.parametrize(("kwargs", "ref"), [
        ({}, "jira:AI-1"),                                # the source item is the thread
        ({"thread": "github:o/r:7"}, "github:o/r:7"),     # the channel names it
    ])
    def test_the_thread_is_written_in_the_insert(self, kwargs, ref):
        ensure, cur, written = self._insert(**kwargs)
        ensure.assert_called_once_with(cur, agent_view_id=3, ref=ref, lock=True)
        assert written["thread_id"] == 6
        assert not any(c.args[0].startswith("UPDATE") for c in cur.execute.call_args_list)

    def test_a_copied_thread_creates_none_and_the_line_columns_pass_through(self):
        ensure, cur, written = self._insert(thread_id=9, parent_job_id=2, depth=1, line_id=4)
        ensure.assert_not_called()
        self.joined.assert_called_once_with(cur, 9, agent_view_id=3, depth=1)
        assert written == {"parent_job_id": 2, "depth": 1, "thread_id": 9, "line_id": 4}

    def test_a_job_with_no_reference_is_its_own_thread_set_after_the_insert(self):
        ensure, cur, written = self._insert(reference_id=None)
        ensure.assert_called_once_with(cur, agent_view_id=3, ref="job:41")
        assert written["thread_id"] is None
        assert cur.execute.call_args.args == ("UPDATE job SET thread_id = %s WHERE id = %s", (6, 41))

    @patch("agento.framework.publisher.get_connection")
    def test_publish_passes_the_thread_arguments_through(self, mock_get_conn, sample_config):
        mock_conn, mock_cursor = _mock_connection(rowcount=1)
        mock_cursor.fetchone.return_value = None
        mock_get_conn.return_value = mock_conn
        with patch("agento.framework.publisher.insert_job", return_value=1) as insert:
            publish(sample_config, AgentType.FOLLOWUP, "jira", "key:t", reference_id="AI-1",
                    thread_id=9, parent_job_id=2, depth=1, line_id=4)
        passed = {k: insert.call_args.kwargs[k] for k in ("thread", *self._COLUMNS)}
        assert passed == {"thread": None, "parent_job_id": 2, "depth": 1, "thread_id": 9,
                          "line_id": 4}
