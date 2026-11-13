"""Jira periodic tasks on the generic schedule (AG-67 plan §5.4, §6), against a real MySQL.

The ``jira_periodic`` source fires through ``tick.fire`` on the issue's thread; ``jira:periodic:sync``
resets the timing columns only when the rule changes; the upgrade check and the data patch turn the
legacy cron rows and map into RRULE, also on an upgrade that enables the module later.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.data_patch import apply_patch, get_all_pending
from agento.framework.module_discovery import scan_all_modules
from agento.framework.module_status import filter_enabled
from agento.framework.schedule import sources, tick
from agento.framework.setup import UpgradeCheckError, run_upgrade_checks
from agento.modules.jira_periodic_tasks.src.config import PeriodicTasksConfig
from agento.modules.jira_periodic_tasks.src.patches.convert_frequency_map_to_rrule import (
    ConvertFrequencyMapToRrule,
)
from agento.modules.jira_periodic_tasks.src.sync import CronEntry, JiraCronSync
from agento.modules.jira_periodic_tasks.src.upgrade_check import FrequencyMapCheck

from .conftest import TEST_DB, _db_connection, _test_connection
from .test_schedule_migration import _drop, _fresh, _jira, _old_shape

AT_9 = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
AT_10 = "FREQ=DAILY;BYHOUR=10;BYMINUTE=0"


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    """``core/scheduler/default_timezone`` is empty: the zone comes from ``TZ`` (hermetic, TST-4)."""
    monkeypatch.setenv("TZ", "UTC")


@pytest.fixture
def conn():
    c = _test_connection()
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def view(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT IGNORE INTO workspace (code, label) VALUES ('jp-ws', 'ws')")
        cur.execute("SELECT id FROM workspace WHERE code = 'jp-ws'")
        ws = cur.fetchone()["id"]
        cur.execute("INSERT IGNORE INTO agent_view (workspace_id, code, label) VALUES (%s, 'jp-av', 'av')", (ws,))
        cur.execute("SELECT id FROM agent_view WHERE code = 'jp-av'")
        av = cur.fetchone()["id"]
    conn.commit()
    return av


def _row(conn, ref: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM schedule WHERE source_ref = %s", (ref,))
        row = cur.fetchone()
    conn.rollback()
    return row


def _sync(int_db_config, view, rule: str, *, title: str = "Report", keys=("AI-2",)) -> int:
    periodic = PeriodicTasksConfig(frequency_map={"Daily": rule})
    syncer = JiraCronSync(MagicMock(), periodic, MagicMock(), logging.getLogger("test"),
                          db_config=int_db_config, agent_view_id=view)
    return syncer._upsert_schedules([CronEntry(k, title, "Daily", rule) for k in keys])


class TestSource:

    def test_the_source_is_registered_and_read_only(self):
        source = sources.get_source("jira_periodic")
        assert source is not None
        assert source.editable is False

    def test_a_jira_row_fires_cron_on_the_issue_thread(self, conn, view):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO schedule (source, source_ref, title, agent_view_id, owner_agent_view_id, "
                        "rrule, starts_at, timezone, next_fire_at) VALUES ('jira_periodic', 'AI-7', 't', %s, %s, "
                        "%s, '2026-06-01', 'UTC', '2026-06-01 09:00')", (view, view, AT_9))
            sid = cur.lastrowid
        conn.commit()

        with conn.cursor() as cur:
            outcome, job_id, run_after = tick.fire(cur, sid, fire_at=datetime(2026, 6, 1, 9), actor="test",
                                                   idempotency_key=f"schedule:{sid}:20260601T0900")
        conn.commit()

        assert outcome is tick.FireOutcome.PUBLISHED
        assert run_after is not None
        with conn.cursor() as cur:
            cur.execute("SELECT j.type, j.source, j.reference_id, j.schedule_id, j.agent_view_id, t.ref "
                        "FROM job j JOIN thread t ON t.id = j.thread_id WHERE j.id = %s", (job_id,))
            job = cur.fetchone()
        conn.rollback()
        assert job == {"type": "cron", "source": "jira", "reference_id": "AI-7", "schedule_id": sid,
                       "agent_view_id": view, "ref": "jira:AI-7"}


class TestSyncWriter:

    def test_sync_writes_a_jira_periodic_row_owned_by_the_view(self, conn, view, int_db_config):
        assert _sync(int_db_config, view, AT_9) == 1
        row = _row(conn, "AI-2")
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        assert {k: row[k] for k in ("source", "title", "agent_view_id", "owner_agent_view_id", "owner_user_id",
                                     "rrule", "starts_at", "timezone", "enabled", "next_fire_at", "cron_expr")} == {
            "source": "jira_periodic", "title": "Report", "agent_view_id": view, "owner_agent_view_id": view,
            "owner_user_id": None, "rrule": AT_9, "starts_at": today, "timezone": "UTC", "enabled": 1,
            "next_fire_at": None, "cron_expr": None}

    def test_a_changed_rule_resets_the_anchor_and_the_next_fire(self, conn, view, int_db_config):
        """Anchor reset on edit (plan §5.1): fire twice at 09:00, move the rule to 10:00, tick."""
        _sync(int_db_config, view, AT_9)
        day0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None) + timedelta(days=1)
        tick.tick(conn, now=day0 + timedelta(hours=8), batch=10)  # fill
        assert _row(conn, "AI-2")["next_fire_at"] == day0 + timedelta(hours=9)
        for day in (day0, day0 + timedelta(days=1)):
            tick.tick(conn, now=day + timedelta(hours=9), batch=10)
        fired = _row(conn, "AI-2")
        assert fired["anchor_local"] == day0 + timedelta(days=1, hours=9)
        assert fired["next_fire_at"] == day0 + timedelta(days=2, hours=9)

        _sync(int_db_config, view, AT_10)
        changed = _row(conn, "AI-2")
        assert (changed["next_fire_at"], changed["anchor_local"]) == (None, None)

        tick.tick(conn, now=day0 + timedelta(days=1, hours=9, minutes=30), batch=10)
        assert _row(conn, "AI-2")["next_fire_at"] == day0 + timedelta(days=1, hours=10)

    def test_an_unchanged_rule_leaves_the_timing_columns_alone(self, conn, view, int_db_config):
        _sync(int_db_config, view, AT_9)
        with conn.cursor() as cur:
            cur.execute("UPDATE schedule SET next_fire_at = '2030-01-02 09:00', anchor_local = '2030-01-01 09:00'")
        conn.commit()

        _sync(int_db_config, view, AT_9, title="Renamed in Jira")
        row = _row(conn, "AI-2")
        assert row["title"] == "Renamed in Jira"
        assert (row["next_fire_at"], row["anchor_local"]) == (datetime(2030, 1, 2, 9), datetime(2030, 1, 1, 9))

    def test_a_row_enabled_again_skips_the_fire_it_missed(self, conn, view, int_db_config):
        _sync(int_db_config, view, AT_9)
        with conn.cursor() as cur:
            cur.execute("UPDATE schedule SET next_fire_at = '2020-01-01 09:00'")
        conn.commit()
        _sync(int_db_config, view, AT_9, keys=())  # left the periodic status: disabled
        assert _row(conn, "AI-2")["enabled"] == 0

        _sync(int_db_config, view, AT_9)  # back again
        row = _row(conn, "AI-2")
        assert (row["enabled"], row["next_fire_at"]) == (1, None)

    def test_the_sweep_leaves_other_sources_of_the_view_alone(self, conn, view, int_db_config):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO schedule (source, title, agent_view_id, owner_agent_view_id, rrule, starts_at, "
                        "timezone) VALUES ('scheduler', 'mine', %s, %s, %s, '2026-06-01', 'UTC')", (view, view, AT_9))
        conn.commit()
        _sync(int_db_config, view, AT_9)

        _sync(int_db_config, view, AT_9, keys=())
        with conn.cursor() as cur:
            cur.execute("SELECT source, enabled FROM schedule ORDER BY id")
            assert cur.fetchall() == [{"source": "scheduler", "enabled": 1}, {"source": "jira_periodic", "enabled": 0}]
        conn.rollback()


def _legacy(conn, view, ref, cron, *, created="2026-01-05 13:00:00", enabled=1) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (source, source_ref, title, agent_view_id, cron_expr, enabled, created_at) "
                    "VALUES ('cron', %s, 't', %s, %s, %s, %s)", (ref, view, cron, enabled, created))
        sid = cur.lastrowid
    conn.commit()
    return sid


class TestDataPatch:

    def test_the_patch_converts_the_map_and_the_legacy_rows(self, conn, view):
        with conn.cursor() as cur:
            for scope, scope_id in (("default", 0), ("agent_view", view)):
                cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value) VALUES (%s, %s, %s, %s)",
                            (scope, scope_id, "jira_periodic_tasks/frequency_map",
                             json.dumps({"Weekly": "0 7 * * 1", "Done": "FREQ=HOURLY;BYMINUTE=0"})))
        conn.commit()
        weekly = _legacy(conn, view, "AI-1", "0 7 * * 1")
        viewless = _legacy(conn, None, "AI-9", "0 8 * * *")

        ConvertFrequencyMapToRrule().apply(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT value FROM core_config_data WHERE path = 'jira_periodic_tasks/frequency_map'")
            maps = [json.loads(r["value"]) for r in cur.fetchall()]
            cur.execute("SELECT id, source, owner_agent_view_id, rrule, starts_at, timezone, next_fire_at, cron_expr, "
                        "enabled, last_error FROM schedule ORDER BY id")
            rows = {r.pop("id"): r for r in cur.fetchall()}
        conn.rollback()
        assert maps == [{"Weekly": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0", "Done": "FREQ=HOURLY;BYMINUTE=0"}] * 2
        common = {"source": "jira_periodic", "starts_at": datetime(2026, 1, 5), "timezone": "UTC",
                  "next_fire_at": None, "cron_expr": None}
        assert rows[weekly] == {**common, "owner_agent_view_id": view,
                                "rrule": "FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0", "enabled": 1, "last_error": None}
        assert rows[viewless] == {**common, "owner_agent_view_id": None, "rrule": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
                                  "enabled": 0, "last_error": "no_agent_view"}

        tick.tick(conn, now=datetime(2026, 6, 1, 12), batch=10)  # Monday 1 June 2026
        assert _row(conn, "AI-1")["next_fire_at"] == datetime(2026, 6, 8, 7)
        assert FrequencyMapCheck().check(conn) == []  # nothing is pending after the patch

    def test_a_disabled_module_converts_on_the_upgrade_that_enables_it(self, conn, view, tmp_path):
        sid = _legacy(conn, view, "AI-1", "0 7 * * 1")

        def pending(status: dict):
            with patch("agento.framework.module_status.read_module_status", return_value=status):
                manifests = filter_enabled(scan_all_modules(CORE_MODULES_DIR, str(tmp_path)))
            return [(m, p) for m, p in get_all_pending(manifests, conn)
                    if p["name"] == "ConvertFrequencyMapToRrule"]

        try:
            assert pending({"jira_periodic_tasks": False}) == []
            tick.tick(conn, now=datetime(2026, 6, 1, 12), batch=10)  # no source matches 'cron': inert
            assert _row(conn, "AI-1")["source"] == "cron"

            (m, p), = pending({})
            apply_patch(m, p, conn, logging.getLogger("test"))
            row = _row(conn, "AI-1")
            assert (row["id"], row["source"], row["cron_expr"]) == (sid, "jira_periodic", None)
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM data_patch WHERE name = 'ConvertFrequencyMapToRrule'")
            conn.commit()


class TestUpgradeCheck:

    @pytest.fixture
    def old_db(self, tmp_path):
        db = f"{TEST_DB}_jp_old"
        _fresh(db)
        c = _db_connection(db)
        yield c
        c.close()
        _drop(db)

    def test_a_fresh_install_is_a_no_op(self, old_db):
        assert FrequencyMapCheck().check(old_db) == []

    def test_after_the_patch_a_bad_map_value_no_longer_refuses_an_upgrade(self, conn):
        """A later ``config:set`` typo is the sync's to skip (§6.4), not a stop for every upgrade."""
        with conn.cursor() as cur:
            cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value) VALUES ('default', 0, %s, %s)",
                        ("jira_periodic_tasks/frequency_map", json.dumps({"Typo": "FREQ=DALY"})))
        conn.commit()
        assert len(FrequencyMapCheck().check(conn)) == 1
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO data_patch (name, module) VALUES "
                            "('ConvertFrequencyMapToRrule', 'jira_periodic_tasks')")
            conn.commit()
            assert FrequencyMapCheck().check(conn) == []
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM data_patch WHERE name = 'ConvertFrequencyMapToRrule'")
            conn.commit()

    def test_an_unconvertible_value_refuses_the_upgrade_before_any_change(self, old_db, tmp_path):
        _old_shape(old_db, tmp_path)
        _jira(old_db)
        with old_db.cursor() as cur:
            cur.execute("INSERT INTO workspace (code, label) VALUES ('w', 'w')")
            cur.execute("INSERT INTO agent_view (workspace_id, code, label) VALUES (%s, 'v', 'v')", (cur.lastrowid,))
            av = cur.lastrowid
            cur.executemany("INSERT INTO schedule (issue_key, agent_view_id, agent_type, cron_expr, enabled) "
                            "VALUES (%s, %s, 'cron', %s, %s)",
                            [("AI-1", av, "0 7 * * 1", 1), ("AI-2", av, "0 0 1 * 1", 1),
                             ("AI-3", av, "0 0 1 * 1", 0), ("AI-4", None, "0 0 1 * 1", 1)])
            cur.execute("INSERT INTO core_config_data (scope, scope_id, path, value) VALUES ('default', 0, %s, %s)",
                        ("jira_periodic_tasks/frequency_map", json.dumps({"Ok": "0 * * * *", "Last": "0 0 L * *"})))
            cur.execute("SELECT COUNT(*) AS n FROM schema_migration")
            before = cur.fetchone()["n"]
        old_db.commit()

        messages = FrequencyMapCheck().check(old_db)
        assert len(messages) == 2  # AI-3 is disabled and AI-4 has no view: the patch disables them
        assert "entry 'Last' ('0 0 L * *')" in messages[0]
        assert "schedule row" in messages[1] and "(AI-2)" in messages[1]

        manifest = next(m for m in scan_all_modules(CORE_MODULES_DIR, str(tmp_path / "none"))
                        if m.name == "jira_periodic_tasks")
        with pytest.raises(UpgradeCheckError, match=r"jira_periodic_tasks: .*AI-2"):
            run_upgrade_checks([manifest], old_db, logging.getLogger("test"))
        with old_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM schema_migration")
            assert cur.fetchone()["n"] == before
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = 'schedule' AND COLUMN_NAME = 'issue_key'")
            assert cur.fetchone()["n"] == 1  # 056 has not run
