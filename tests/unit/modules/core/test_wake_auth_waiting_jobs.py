"""WI-6: a new or refreshed credential wakes the jobs that wait for one."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from agento.modules.core.src import observers
from agento.modules.core.src.observers import (
    RefreshCredentialLimitsObserver,
    WakeAuthWaitingJobsObserver,
    wake_auth_waiting_jobs,
)


def _conn(ids):
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [{"id": i} for i in ids]
    return conn, cur


def test_wake_reads_waiting_jobs_in_id_order_bounded_then_updates_only_those():
    conn, cur = _conn([3, 7, 9])

    assert wake_auth_waiting_jobs(conn) == 3

    (select_sql, select_args), (update_sql, update_args) = (c.args for c in cur.execute.call_args_list)
    assert "status = 'TODO' AND error_class = %s ORDER BY id LIMIT %s" in select_sql
    assert "FOR UPDATE" not in select_sql  # a consistent read never waits for locks
    assert select_args == ("CredentialsUnavailableError", 200)
    assert "LEAST(scheduled_after, NOW())" in update_sql
    assert "WHERE id IN (%s, %s, %s) AND status = 'TODO' AND error_class = %s" in update_sql
    assert update_args == (3, 7, 9, "CredentialsUnavailableError")
    conn.commit.assert_not_called()


def test_wake_with_no_waiting_job_writes_nothing():
    conn, cur = _conn([])
    assert wake_auth_waiting_jobs(conn) == 0
    assert cur.execute.call_count == 1


def test_observer_commits_and_never_crashes_the_caller():
    conn, _cur = _conn([1])
    pool = MagicMock()
    pool.return_value.__enter__.return_value = conn
    with patch.object(observers, "pooled", pool), patch.object(observers, "DatabaseConfig"):
        WakeAuthWaitingJobsObserver().execute(MagicMock())
    conn.commit.assert_called_once()

    pool.side_effect = RuntimeError("db down")
    with patch.object(observers, "pooled", pool), patch.object(observers, "DatabaseConfig"):
        WakeAuthWaitingJobsObserver().execute(MagicMock())  # no raise


def test_core_declares_the_observer_on_both_credential_events():
    events = json.loads(
        (Path(observers.__file__).parents[1] / "events.json").read_text())
    for name in ("credential_register_after", "credential_refresh_after"):
        assert [o["class"] for o in events[name]] == [
            "src.observers.WakeAuthWaitingJobsObserver", "src.observers.RefreshCredentialLimitsObserver"]


def test_limits_observer_reads_only_the_event_credential_and_never_crashes_the_caller(caplog):
    conn = MagicMock()
    pool = MagicMock()
    pool.return_value.__enter__.return_value = conn
    event = MagicMock(credential_id=7, credentials={"subscription_key": "sk-secret"})
    with patch.object(observers, "pooled", pool), patch.object(observers, "DatabaseConfig"), \
            patch("agento.framework.cli.credential.refresh_credential_limits") as refresh:
        RefreshCredentialLimitsObserver().execute(event)
        refresh.assert_called_once_with(conn, observers.logger, credential_id=7)

        refresh.side_effect = RuntimeError("sk-secret")
        RefreshCredentialLimitsObserver().execute(event)  # no raise
    assert "id=7" in caplog.text and "RuntimeError" in caplog.text
    assert "sk-secret" not in caplog.text
