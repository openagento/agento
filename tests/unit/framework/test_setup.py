"""Tests for setup:upgrade orchestrator (Phase 7)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.event_manager import ObserverEntry, get_event_manager
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.events import CrontabInstalledEvent, SetupBeforeEvent, SetupCompleteEvent
from agento.framework.setup import SetupResult, setup_upgrade


class _EventCollector:
    events: list = []  # noqa: RUF012

    def execute(self, event: object) -> None:
        _EventCollector.events.append(event)

    @classmethod
    def reset(cls):
        cls.events = []


@pytest.fixture(autouse=True)
def _ignore_host_module_status():
    """These tests build fake modules in tmp_path; the HOST's app/etc/modules.json
    must not decide whether they count as enabled. Without this, disabling a module
    locally (e.g. `agento module:disable jira`) silently skips the fake module of
    the same name and the assertions fail on unrelated deployment state."""
    with patch("agento.framework.module_status.read_module_status", return_value={}):
        yield


@pytest.fixture(autouse=True)
def _clean_events():
    clear_event_manager()
    _EventCollector.reset()
    yield
    clear_event_manager()


def _mock_conn(fetchall_return=None):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = fetchall_return or []
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


def _setup_modules(tmp_path: Path) -> tuple[str, str]:
    """Create core and user module dirs for testing."""
    core_dir = tmp_path / "core_modules"
    user_dir = tmp_path / "user_modules"
    core_dir.mkdir()
    user_dir.mkdir()
    return str(core_dir), str(user_dir)


class TestSetupResult:
    def test_has_work_empty(self):
        assert not SetupResult().has_work

    def test_has_work_with_migrations(self):
        assert SetupResult(framework_migrations=["001"]).has_work

    def test_has_work_with_cron(self):
        assert SetupResult(cron_changed=True).has_work

    def test_has_work_with_patches(self):
        assert SetupResult(data_patches={"jira": ["Pop"]}).has_work


class TestSetupUpgrade:
    @patch("agento.framework.setup.install_crontab", return_value=False)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=["011_module_migrations"])
    def test_applies_framework_migrations(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        conn, _ = _mock_conn()
        core_dir, user_dir = _setup_modules(tmp_path)
        logger = logging.getLogger("test")

        result = setup_upgrade(conn, logger, core_dir=core_dir, user_dir=user_dir)

        assert result.framework_migrations == ["011_module_migrations"]
        mock_migrate.assert_called_once()
        assert mock_migrate.call_args[1]["module"] == "framework"

    @patch("agento.framework.setup.install_crontab", return_value=False)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate")
    def test_module_migrations_in_dependency_order(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        core_dir, user_dir = _setup_modules(tmp_path)

        # Create two modules with sql/ dirs
        for name, order in [("core", 1), ("jira", 100)]:
            mod = Path(core_dir) / name
            mod.mkdir()
            (mod / "module.json").write_text(json.dumps({
                "name": name, "version": "1.0.0", "description": f"{name} module",
                "order": order, "sequence": ["core"] if name == "jira" else [],
            }))
            sql = mod / "sql"
            sql.mkdir()
            (sql / "001_init.sql").write_text("SELECT 1;")

        # Framework returns nothing, module calls return version
        mock_migrate.side_effect = [
            [],  # framework
            ["001_init"],  # core
            ["001_init"],  # jira
        ]
        conn, _ = _mock_conn()

        result = setup_upgrade(conn, logging.getLogger("test"), core_dir=core_dir, user_dir=user_dir)

        # Verify module migrations were called in order
        module_calls = [c for c in mock_migrate.call_args_list if c[1].get("module") != "framework"]
        assert module_calls[0][1]["module"] == "core"
        assert module_calls[1][1]["module"] == "jira"
        assert result.module_migrations == {"core": ["001_init"], "jira": ["001_init"]}

    @patch("agento.framework.setup.migrate")
    def test_invalid_manifest_aborts_before_migrations(self, mock_migrate, tmp_path):
        import pytest

        from agento.framework.setup import ModuleValidationError

        core_dir, user_dir = _setup_modules(tmp_path)
        # Enabled module whose tool is missing the required 'toolset'.
        mod = Path(core_dir) / "bad"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps({
            "name": "bad", "version": "1.0.0", "description": "bad module",
            "tools": [{"type": "mysql", "name": "mysql_x", "description": "x"}],
        }))
        conn, _ = _mock_conn()

        with pytest.raises(ModuleValidationError):
            setup_upgrade(conn, logging.getLogger("test"), core_dir=core_dir, user_dir=user_dir)
        mock_migrate.assert_not_called()  # aborts before any migration

    @patch("agento.framework.setup.install_crontab", return_value=False)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=[])
    @patch("agento.framework.setup.get_pending", return_value=[])
    def test_dry_run_no_mutations(self, mock_pending, mock_migrate, mock_crontab, mock_install, tmp_path):
        conn, _ = _mock_conn()
        core_dir, user_dir = _setup_modules(tmp_path)

        result = setup_upgrade(
            conn, logging.getLogger("test"),
            dry_run=True, core_dir=core_dir, user_dir=user_dir,
        )

        # migrate() should NOT be called in dry-run — only get_pending()
        mock_migrate.assert_not_called()
        assert not result.has_work

    @patch("agento.framework.setup.install_crontab", return_value=True)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=[])
    def test_cron_installation(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        core_dir, user_dir = _setup_modules(tmp_path)

        # Module with cron.json
        mod = Path(core_dir) / "jira"
        mod.mkdir()
        (mod / "module.json").write_text(json.dumps(
            {"name": "jira", "version": "1.0.0", "description": "jira module"}
        ))
        (mod / "cron.json").write_text(json.dumps({
            "jobs": [{"name": "sync", "schedule": "0 * * * *", "command": "sync"}]
        }))

        conn, _ = _mock_conn()
        result = setup_upgrade(conn, logging.getLogger("test"), core_dir=core_dir, user_dir=user_dir)

        assert result.cron_changed
        mock_install.assert_called_once()


class TestSetupEvents:
    @patch("agento.framework.setup.install_crontab", return_value=False)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=[])
    def test_dispatches_setup_before_and_complete(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        em = get_event_manager()
        em.register("setup_upgrade_before", ObserverEntry(name="b", observer_class=_EventCollector))
        em.register("setup_upgrade_after", ObserverEntry(name="c", observer_class=_EventCollector))

        conn, _ = _mock_conn()
        core_dir, user_dir = _setup_modules(tmp_path)
        setup_upgrade(conn, logging.getLogger("test"), core_dir=core_dir, user_dir=user_dir)

        types = [type(e) for e in _EventCollector.events]
        assert SetupBeforeEvent in types
        assert SetupCompleteEvent in types
        # before fires first
        assert types.index(SetupBeforeEvent) < types.index(SetupCompleteEvent)

    @patch("agento.framework.setup.install_crontab", return_value=False)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=[])
    def test_setup_before_carries_dry_run(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        em = get_event_manager()
        em.register("setup_upgrade_before", ObserverEntry(name="b", observer_class=_EventCollector))

        conn, _ = _mock_conn()
        core_dir, user_dir = _setup_modules(tmp_path)
        setup_upgrade(conn, logging.getLogger("test"), dry_run=True, core_dir=core_dir, user_dir=user_dir)

        assert len(_EventCollector.events) >= 1
        evt = _EventCollector.events[0]
        assert isinstance(evt, SetupBeforeEvent)
        assert evt.dry_run is True

    @patch("agento.framework.setup.install_crontab", return_value=True)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=[])
    def test_dispatches_crontab_installed_on_change(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        em = get_event_manager()
        em.register("crontab_install_after", ObserverEntry(name="ci", observer_class=_EventCollector))

        core_dir, user_dir = _setup_modules(tmp_path)
        conn, _ = _mock_conn()
        setup_upgrade(conn, logging.getLogger("test"), core_dir=core_dir, user_dir=user_dir)

        cron_events = [e for e in _EventCollector.events if isinstance(e, CrontabInstalledEvent)]
        assert len(cron_events) == 1

    @patch("agento.framework.setup.install_crontab", return_value=False)
    @patch("agento.framework.setup.get_current_crontab", return_value="")
    @patch("agento.framework.setup.migrate", return_value=[])
    def test_no_crontab_event_when_unchanged(self, mock_migrate, mock_crontab, mock_install, tmp_path):
        em = get_event_manager()
        em.register("crontab_install_after", ObserverEntry(name="ci", observer_class=_EventCollector))

        core_dir, user_dir = _setup_modules(tmp_path)
        conn, _ = _mock_conn()
        setup_upgrade(conn, logging.getLogger("test"), core_dir=core_dir, user_dir=user_dir)

        cron_events = [e for e in _EventCollector.events if isinstance(e, CrontabInstalledEvent)]
        assert len(cron_events) == 0


class _FakeOnboarding:
    """Fake onboarding for testing strict flow."""

    def __init__(self, complete_after: int = 1):
        self._call_count = 0
        self._complete_after = complete_after
        self._description = "Configure test module"

    def is_complete(self, conn) -> bool:
        return self._call_count >= self._complete_after

    def run(self, conn, config, logger) -> None:
        self._call_count += 1

    def describe(self) -> str:
        return self._description


class TestStrictOnboarding:
    """Tests for the strict onboarding flow in setup:upgrade."""

    def _run(self, tmp_path, onboardings, select_returns, all_scanned=None):
        """Helper: run setup_upgrade with mocked onboarding + select."""
        core_dir, user_dir = _setup_modules(tmp_path)
        conn, _ = _mock_conn()

        with patch("agento.framework.setup.install_crontab", return_value=False), \
             patch("agento.framework.setup.get_current_crontab", return_value=""), \
             patch("agento.framework.setup.migrate", return_value=[]), \
             patch("agento.framework.setup.validate_module", return_value=[]), \
             patch("agento.framework.onboarding.get_onboardings", return_value=onboardings), \
             patch("agento.framework.bootstrap.get_module_config", return_value={}), \
             patch("agento.framework.cli.terminal.select", side_effect=select_returns) as mock_select, \
             patch("agento.framework.module_status.set_enabled") as mock_set_enabled:

            # If we need custom all_scanned manifests, mock scan_modules
            if all_scanned is not None:
                # setup now goes through the ONE shared discovery path (which also covers
                # PyPI extensions mounted outside core/user dirs).
                with patch(
                    "agento.framework.setup.scan_all_modules",
                    return_value=all_scanned,
                ):
                    result = setup_upgrade(conn, logging.getLogger("test"),
                                         core_dir=core_dir, user_dir=user_dir)
            else:
                result = setup_upgrade(conn, logging.getLogger("test"),
                                     core_dir=core_dir, user_dir=user_dir)

        return result, mock_select, mock_set_enabled

    def test_onboarding_succeeds_first_try(self, tmp_path):
        """User selects 'Proceed', onboarding completes on first run."""
        onboarding = _FakeOnboarding(complete_after=1)
        result, _mock_select, _ = self._run(
            tmp_path,
            onboardings={"jira": onboarding},
            select_returns=[0],  # Proceed
        )
        assert "jira" in result.onboardings_run
        assert result.onboardings_disabled == []

    def test_onboarding_fails_retry_succeeds(self, tmp_path):
        """Onboarding fails first time, user retries, succeeds."""
        onboarding = _FakeOnboarding(complete_after=2)
        result, _mock_select, _ = self._run(
            tmp_path,
            onboardings={"jira": onboarding},
            select_returns=[0, 0],  # Proceed, then Retry
        )
        assert "jira" in result.onboardings_run
        assert result.onboardings_disabled == []

    def test_user_disables_module(self, tmp_path):
        """User skips, then disables module."""
        onboarding = _FakeOnboarding(complete_after=999)  # never completes
        result, _, mock_set_enabled = self._run(
            tmp_path,
            onboardings={"jira": onboarding},
            select_returns=[1, 1],  # Skip, then Disable
        )
        assert "jira" in result.onboardings_disabled
        mock_set_enabled.assert_any_call("jira", False)

    def test_user_disables_module_with_dependents(self, tmp_path):
        """Disabling a module also disables its transitive dependents."""
        from agento.framework.module_loader import ModuleManifest

        jira_onboarding = _FakeOnboarding(complete_after=999)
        manifests = [
            ModuleManifest(name="jira", version="1.0.0", description="", path=Path("/fake/jira")),
            ModuleManifest(name="jira_periodic_tasks", version="1.0.0", description="",
                          path=Path("/fake/jira_periodic_tasks"), sequence=["jira"]),
        ]

        result, _, mock_set_enabled = self._run(
            tmp_path,
            onboardings={"jira": jira_onboarding},
            select_returns=[1, 1],  # Skip, then Disable
            all_scanned=manifests,
        )
        assert "jira" in result.onboardings_disabled
        assert "jira_periodic_tasks" in result.onboardings_disabled
        mock_set_enabled.assert_any_call("jira", False)
        mock_set_enabled.assert_any_call("jira_periodic_tasks", False)

    def test_user_quits(self, tmp_path):
        """User selects Quit -> SystemExit raised."""
        onboarding = _FakeOnboarding(complete_after=999)
        with pytest.raises(SystemExit):
            self._run(
                tmp_path,
                onboardings={"jira": onboarding},
                select_returns=[1, 2],  # Skip, then Quit
            )

    def test_disabled_dependents_skipped_in_subsequent_iterations(self, tmp_path):
        """After disabling jira, jira_periodic_tasks is skipped even if it has onboarding."""
        from agento.framework.module_loader import ModuleManifest

        jira_onboarding = _FakeOnboarding(complete_after=999)
        jpt_onboarding = _FakeOnboarding(complete_after=999)

        manifests = [
            ModuleManifest(name="jira", version="1.0.0", description="", path=Path("/fake/jira")),
            ModuleManifest(name="jira_periodic_tasks", version="1.0.0", description="",
                          path=Path("/fake/jira_periodic_tasks"), sequence=["jira"]),
        ]

        result, mock_select, _ = self._run(
            tmp_path,
            onboardings={"jira": jira_onboarding, "jira_periodic_tasks": jpt_onboarding},
            select_returns=[1, 1],  # Skip jira, Disable jira
            all_scanned=manifests,
        )
        # jira_periodic_tasks should NOT trigger any select call since it was disabled with jira
        assert mock_select.call_count == 2  # only the 2 calls for jira
        assert "jira_periodic_tasks" in result.onboardings_disabled
