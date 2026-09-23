from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira_periodic_tasks.src.sync import CronEntry, JiraCronSync


def test_build_jql_without_assignee(sample_config, sample_periodic_config):
    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    jql = syncer.build_jql()
    assert jql == 'project = AI AND status = "Cykliczne"'


def test_build_jql_falls_back_to_assignee_when_no_account_id(sample_periodic_config):
    config = JiraConfig(
        toolbox_url="http://toolbox:3001",
        user="bot@test.com",
        jira_projects=["AI"],
        jira_assignee="bot@test.com",
    )
    syncer = JiraCronSync(config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    jql = syncer.build_jql()
    assert 'AND assignee = "bot@test.com"' in jql


def test_build_jql_prefers_account_id_over_display_name(sample_periodic_config):
    """When both are set, accountId wins — Jira Cloud silently returns 0 results
    for `assignee = "<display name>"` so accountId is the only reliable form."""
    config = JiraConfig(
        toolbox_url="http://toolbox:3001",
        user="bot@test.com",
        jira_projects=["AI"],
        jira_assignee="Mieszko",
        jira_assignee_account_id="712020:abc-def-123",
    )
    syncer = JiraCronSync(config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    jql = syncer.build_jql()
    assert 'AND assignee = "712020:abc-def-123"' in jql
    assert "Mieszko" not in jql


def test_build_jql_no_account_id_no_assignee_omits_clause(sample_periodic_config):
    config = JiraConfig(
        toolbox_url="http://toolbox:3001",
        jira_projects=["AI"],
    )
    syncer = JiraCronSync(config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    jql = syncer.build_jql()
    assert "assignee" not in jql


def test_parse_issues_valid(sample_config, sample_periodic_config, jira_cykliczne):
    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    entries = syncer.parse_issues(jira_cykliczne)

    # AI-2 (Co 5min) and AI-3 (1x dziennie o 8:00) should be parsed
    # AI-4 (null frequency) and AI-5 (unknown frequency) should be skipped
    assert len(entries) == 2
    assert entries[0].issue_key == "AI-2"
    assert entries[0].cron_expression == "*/5 * * * *"
    assert entries[1].issue_key == "AI-3"
    assert entries[1].cron_expression == "0 8 * * *"


def test_parse_issues_skip_null_frequency(sample_config, sample_periodic_config, jira_cykliczne, caplog):
    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    with caplog.at_level(logging.WARNING):
        syncer.parse_issues(jira_cykliczne)

    assert any("AI-4" in r.message and "no frequency" in r.message for r in caplog.records)


def test_parse_issues_skip_unknown_frequency(sample_config, sample_periodic_config, jira_cykliczne, caplog):
    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    with caplog.at_level(logging.WARNING):
        syncer.parse_issues(jira_cykliczne)

    assert any("AI-5" in r.message and "unknown frequency" in r.message for r in caplog.records)


def test_parse_issues_empty(sample_config, sample_periodic_config, jira_empty):
    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    entries = syncer.parse_issues(jira_empty)
    assert entries == []


# ---- Schedules upsert tests ----


def _mock_connection():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


def _sample_entries():
    return [
        CronEntry(issue_key="AI-2", summary="Task 2", frequency_label="Co 5min", cron_expression="*/5 * * * *"),
        CronEntry(issue_key="AI-3", summary="Task 3", frequency_label="1x dziennie o 8:00", cron_expression="0 8 * * *"),
    ]


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_inserts(mock_get_conn, sample_config, sample_periodic_config, jira_cykliczne):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    entries = _sample_entries()

    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    syncer._upsert_schedules(entries)

    # One INSERT per entry + one UPDATE for removed entries
    assert mock_cursor.execute.call_count == 3
    first_sql = mock_cursor.execute.call_args_list[0][0][0]
    assert "INSERT INTO schedule" in first_sql
    assert "ON DUPLICATE KEY UPDATE" in first_sql
    mock_conn.commit.assert_called_once()


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_disables_removed(mock_get_conn, sample_config, sample_periodic_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    entries = _sample_entries()

    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    syncer._upsert_schedules(entries)

    # Last call should be the UPDATE for non-listed keys
    last_call = mock_cursor.execute.call_args_list[-1]
    sql = last_call[0][0]
    assert "enabled = FALSE" in sql
    assert "NOT IN" in sql


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_empty_disables_all(mock_get_conn, sample_config, sample_periodic_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    syncer._upsert_schedules([])

    # With empty entries, should disable all schedules
    mock_cursor.execute.assert_called_once()
    sql = mock_cursor.execute.call_args[0][0]
    assert "enabled = FALSE" in sql
    assert "NOT IN" not in sql


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_writes_agent_view_id(mock_get_conn, sample_config, sample_periodic_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    syncer = JiraCronSync(
        sample_config, sample_periodic_config, MagicMock(),
        logging.getLogger("test"), agent_view_id=42, agent_view_code="mieszko",
    )
    syncer._upsert_schedules([_sample_entries()[0]])

    insert_call = mock_cursor.execute.call_args_list[0]
    sql = insert_call[0][0]
    params = insert_call[0][1]
    assert "agent_view_id" in sql
    assert params[0] == 42


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_scoped_disable_sweep_per_agent_view(
    mock_get_conn, sample_config, sample_periodic_config
):
    """mieszko's sync must never disable zyga's rows."""
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    syncer = JiraCronSync(
        sample_config, sample_periodic_config, MagicMock(),
        logging.getLogger("test"), agent_view_id=7, agent_view_code="mieszko",
    )
    syncer._upsert_schedules(_sample_entries())

    disable_sql = mock_cursor.execute.call_args_list[-1][0][0]
    disable_params = mock_cursor.execute.call_args_list[-1][0][1]
    assert "agent_view_id = %s" in disable_sql
    assert "NOT IN" in disable_sql
    assert disable_params[0] == 7


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_scoped_disable_when_empty_per_agent_view(
    mock_get_conn, sample_config, sample_periodic_config
):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    syncer = JiraCronSync(
        sample_config, sample_periodic_config, MagicMock(),
        logging.getLogger("test"), agent_view_id=9, agent_view_code="zyga",
    )
    syncer._upsert_schedules([])

    sql = mock_cursor.execute.call_args[0][0]
    params = mock_cursor.execute.call_args[0][1]
    assert "agent_view_id = %s" in sql
    assert "NOT IN" not in sql
    assert params == (9,)


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_null_agent_view_uses_is_null_predicate(
    mock_get_conn, sample_config, sample_periodic_config
):
    """Fallback path (no agent_views configured) must scope to IS NULL rows only."""
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    syncer = JiraCronSync(
        sample_config, sample_periodic_config, MagicMock(),
        logging.getLogger("test"),
    )
    syncer._upsert_schedules(_sample_entries())

    disable_sql = mock_cursor.execute.call_args_list[-1][0][0]
    assert "agent_view_id IS NULL" in disable_sql


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_db_error_logged(mock_get_conn, sample_config, sample_periodic_config, caplog):
    mock_conn, mock_cursor = _mock_connection()
    mock_cursor.execute.side_effect = RuntimeError("DB error")
    mock_get_conn.return_value = mock_conn

    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))

    with caplog.at_level(logging.WARNING):
        syncer._upsert_schedules(_sample_entries())

    mock_conn.rollback.assert_called_once()


def test_sync_view_emits_summary_line(sample_config, sample_periodic_config, jira_cykliczne, caplog):
    toolbox = MagicMock()
    toolbox.jira_search.return_value = jira_cykliczne

    logger = logging.getLogger("test-sync-summary")
    syncer = JiraCronSync(sample_config, sample_periodic_config, toolbox, logger)

    with caplog.at_level(logging.INFO):
        syncer.sync_view(dry_run=True)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert info_records[0].message.startswith("Sync OK")
    assert "2 entries" in info_records[0].message


@patch("agento.modules.jira_periodic_tasks.src.sync.get_connection")
def test_upsert_schedules_skipped_in_dry_run(mock_get_conn, sample_config, sample_periodic_config, jira_cykliczne):
    toolbox = MagicMock()
    toolbox.jira_search.return_value = jira_cykliczne

    syncer = JiraCronSync(sample_config, sample_periodic_config, toolbox, logging.getLogger("test"))
    syncer.sync_view(dry_run=True)

    # get_connection should NOT have been called (upsert skipped in dry_run)
    mock_get_conn.assert_not_called()
