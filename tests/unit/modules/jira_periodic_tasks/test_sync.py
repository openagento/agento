from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from agento.modules.jira.src.config import JiraConfig
from agento.modules.jira_periodic_tasks.src.config import PeriodicTasksConfig
from agento.modules.jira_periodic_tasks.src.sync import CronEntry, JiraCronSync

SYNC = "agento.modules.jira_periodic_tasks.src.sync"


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
    assert entries[0].rrule == "FREQ=HOURLY;BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55"
    assert entries[1].issue_key == "AI-3"
    assert entries[1].rrule == "FREQ=DAILY;BYHOUR=8;BYMINUTE=0"


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


@pytest.fixture(autouse=True)
def _core_settings():
    """The core scheduler settings the upsert reads from its connection."""
    with patch(f"{SYNC}.min_interval_minutes", return_value=5), patch(f"{SYNC}.default_timezone", return_value="UTC"):
        yield


def _mock_connection():
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cursor


def _sample_entries():
    return [
        CronEntry(issue_key="AI-2", summary="Task 2", frequency_label="Co 5min",
                  rrule="FREQ=HOURLY;BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55"),
        CronEntry(issue_key="AI-3", summary="Task 3", frequency_label="1x dziennie o 8:00",
                  rrule="FREQ=DAILY;BYHOUR=8;BYMINUTE=0"),
    ]


def _syncer(sample_config, periodic_config, view=7):
    return JiraCronSync(sample_config, periodic_config, MagicMock(), logging.getLogger("test"),
                        agent_view_id=view, agent_view_code="mieszko")


@patch(f"{SYNC}.get_connection")
def test_upsert_writes_a_jira_periodic_row_owned_by_the_view(mock_get_conn, sample_config, sample_periodic_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    _syncer(sample_config, sample_periodic_config, view=42)._upsert_schedules(_sample_entries())

    # One INSERT per entry + one disable sweep
    assert mock_cursor.execute.call_count == 3
    sql, params = mock_cursor.execute.call_args_list[0][0]
    assert "INSERT INTO schedule" in sql
    assert params[:5] == ("jira_periodic", "AI-2", "Task 2", 42, 42)
    assert params[5] == "FREQ=HOURLY;BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55"
    mock_conn.commit.assert_called_once()


@pytest.mark.parametrize("entries, not_in", [(_sample_entries(), True), ([], False)])
@patch(f"{SYNC}.get_connection")
def test_the_disable_sweep_touches_only_this_views_jira_rows(mock_get_conn, entries, not_in, sample_config,
                                                              sample_periodic_config):
    """mieszko's sync never disables zyga's rows, nor a row of another source in its own view."""
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    _syncer(sample_config, sample_periodic_config)._upsert_schedules(entries)

    sql, params = mock_cursor.execute.call_args_list[-1][0]
    assert "enabled = 0" in sql
    assert "agent_view_id = %s AND source = %s" in sql
    assert params[:2] == (7, "jira_periodic")
    assert ("NOT IN" in sql) is not_in


@patch(f"{SYNC}.get_connection")
def test_a_sync_without_a_view_writes_nothing(mock_get_conn, sample_config, sample_periodic_config):
    syncer = JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"))
    assert syncer._upsert_schedules(_sample_entries()) == 0
    mock_get_conn.assert_not_called()


@patch(f"{SYNC}.get_connection")
def test_a_cron_map_value_is_refused_and_its_rows_are_disabled(mock_get_conn, sample_config, caplog):
    """Only an ENV override can still hold cron after the upgrade patch (AG-67 No cron compat)."""
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn
    periodic = PeriodicTasksConfig(frequency_map={"Co 5min": "*/5 * * * *"})
    entry = CronEntry(issue_key="AI-2", summary="t", frequency_label="Co 5min", rrule="*/5 * * * *")

    with caplog.at_level(logging.ERROR):
        assert _syncer(sample_config, periodic)._upsert_schedules([entry]) == 0

    assert "frequency_map entry 'Co 5min' is not an RRULE; cron is no longer accepted (AG-67)" in caplog.text
    assert mock_cursor.execute.call_count == 1  # the sweep only: AI-2 is disabled
    assert "NOT IN" not in mock_cursor.execute.call_args[0][0]


@patch(f"{SYNC}.get_connection")
def test_a_rule_the_engine_refuses_is_not_written(mock_get_conn, sample_config, caplog):
    mock_conn, _ = _mock_connection()
    mock_get_conn.return_value = mock_conn
    periodic = PeriodicTasksConfig(frequency_map={"Fast": "FREQ=MINUTELY;INTERVAL=1"})
    entry = CronEntry(issue_key="AI-2", summary="t", frequency_label="Fast", rrule="FREQ=MINUTELY;INTERVAL=1")

    with caplog.at_level(logging.ERROR):
        assert _syncer(sample_config, periodic)._upsert_schedules([entry]) == 0
    assert "frequency_map entry 'Fast' is refused: too_frequent" in caplog.text


@patch(f"{SYNC}.engine.validate")
@patch(f"{SYNC}.get_connection")
def test_each_map_value_is_validated_once_per_shared_cache(mock_get_conn, validate, sample_config,
                                                           sample_periodic_config):
    mock_get_conn.side_effect = lambda *_: _mock_connection()[0]
    rules: dict = {}
    for view in (1, 2):
        JiraCronSync(sample_config, sample_periodic_config, MagicMock(), logging.getLogger("test"),
                     agent_view_id=view, rules=rules)._upsert_schedules(_sample_entries())
    assert validate.call_count == 2  # two labels, two views


@patch(f"{SYNC}.min_interval_minutes", return_value=5)
@patch(f"{SYNC}.engine.validate")
def test_a_shared_cache_keeps_each_views_own_rule_for_one_label(_validate, _min, sample_config):
    """Scoped maps may give one label different rules: the cache is keyed by the rule (CFG-1)."""
    from datetime import datetime

    rules: dict = {}
    got = [JiraCronSync(sample_config, PeriodicTasksConfig(frequency_map={"Daily": value}), MagicMock(),
                        logging.getLogger("test"), agent_view_id=view, rules=rules)
           ._checked_rule(MagicMock(), "Daily", datetime(2027, 1, 1), "UTC")
           for view, value in ((1, "FREQ=DAILY;BYHOUR=9"), (2, "FREQ=DAILY;BYHOUR=17"))]
    assert got == ["FREQ=DAILY;BYHOUR=9", "FREQ=DAILY;BYHOUR=17"]


@patch(f"{SYNC}.engine.validate", side_effect=RuntimeError("schedule validator failed"))
@patch(f"{SYNC}.get_connection")
def test_a_validator_crash_writes_nothing_and_disables_nothing(mock_get_conn, _validate, sample_config,
                                                               sample_periodic_config):
    mock_conn, mock_cursor = _mock_connection()
    mock_get_conn.return_value = mock_conn

    assert _syncer(sample_config, sample_periodic_config)._upsert_schedules(_sample_entries()) == 0
    mock_cursor.execute.assert_not_called()
    mock_conn.rollback.assert_called_once()


@patch(f"{SYNC}.get_connection")
def test_upsert_schedules_db_error_logged(mock_get_conn, sample_config, sample_periodic_config, caplog):
    mock_conn, mock_cursor = _mock_connection()
    mock_cursor.execute.side_effect = RuntimeError("DB error")
    mock_get_conn.return_value = mock_conn

    with caplog.at_level(logging.WARNING):
        _syncer(sample_config, sample_periodic_config)._upsert_schedules(_sample_entries())

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


@patch(f"{SYNC}.get_connection")
def test_upsert_schedules_skipped_in_dry_run(mock_get_conn, sample_config, sample_periodic_config, jira_cykliczne):
    toolbox = MagicMock()
    toolbox.jira_search.return_value = jira_cykliczne

    syncer = JiraCronSync(sample_config, sample_periodic_config, toolbox, logging.getLogger("test"))
    syncer.sync_view(dry_run=True)

    # get_connection should NOT have been called (upsert skipped in dry_run)
    mock_get_conn.assert_not_called()
