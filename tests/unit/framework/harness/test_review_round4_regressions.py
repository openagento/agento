"""Regression tests for impl review round 4.

Theme: contract edges that only a *third-party* harness would hit — a long id, a
secret-only provider, an immutable runner, a PyPI-installed module. The two shipped
harnesses happen to avoid all four.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.harness import (
    Runner,
    RunRequest,
    RunResult,
    clear,
)
from tests.harness_fixtures import register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]
SQL = REPO / "src" / "agento" / "framework" / "sql"


class TestHarnessIdFitsTheJobColumn:
    """Ids may be 64 chars (matching ``credential.scope``), but ``job.agent_type`` was
    still the pre-0.15 ``VARCHAR(20)`` sized for 'claude'/'codex'. A valid 21-64 char
    third-party id would fail the SUCCESS UPDATE at job finalization."""

    def test_id_max_length_is_64(self):
        from agento.framework.harness.descriptor import ID_MAX_LENGTH

        assert ID_MAX_LENGTH == 64

    def test_upgrade_path_widens_the_column(self):
        migration = (SQL / "031_job_provider.sql").read_text()
        assert re.search(
            r"ALTER TABLE job\s+MODIFY agent_type VARCHAR\(64\)", migration,
        ), "migration 031 must widen job.agent_type to match the id contract"

    def test_fresh_install_schema_matches(self):
        init = (SQL / "init" / "000_init.sql").read_text()
        job_block = init[init.index("CREATE TABLE IF NOT EXISTS job"):]
        job_block = job_block[: job_block.index(";")]

        m = re.search(r"agent_type\s+VARCHAR\((\d+)\)", job_block)
        assert m is not None and int(m.group(1)) == 64, job_block

    def test_provider_column_is_also_64(self):
        """Provider ids share the same id rules, so they need the same room."""
        init = (SQL / "init" / "000_init.sql").read_text()
        job_block = init[init.index("CREATE TABLE IF NOT EXISTS job"):]
        job_block = job_block[: job_block.index(";")]
        assert re.search(r"provider\s+VARCHAR\(64\)", job_block)

    def test_a_max_length_id_round_trips_through_the_job_model(self):
        from agento.framework.harness.descriptor import ID_MAX_LENGTH, HarnessDescriptor
        from agento.framework.job_models import Job

        long_id = "h" + "a" * (ID_MAX_LENGTH - 1)
        descriptor = HarnessDescriptor.from_declaration({
            "id": long_id, "label": "L", "default_provider": "p",
            "providers": [{"id": "p", "credential_required": False}],
        })
        assert len(str(descriptor.id)) == ID_MAX_LENGTH

        row = {
            "id": 1, "schedule_id": None, "type": "cron", "source": "blank",
            "agent_view_id": None, "priority": 50, "reference_id": "X",
            "agent_type": long_id, "provider": "p", "model": None,
            "input_tokens": None, "output_tokens": None, "prompt": None, "output": None,
            "context": None, "idempotency_key": "k", "status": "SUCCESS", "attempt": 1,
            "max_attempts": 3, "scheduled_after": None, "started_at": None,
            "finished_at": None, "result_summary": None, "error_message": None,
            "error_class": None, "pid": None, "session_id": None,
            "created_at": None, "updated_at": None,
        }
        assert Job.from_row(row).agent_type == long_id


class TestInstallHonoursRegistrationModes:
    """The wizard drove interactive OAuth unconditionally, so a provider declaring only
    api_key/access_token (the fixture's ``fake_cloud``) could not be onboarded at all."""

    def _provider(self, modes: list[str]):
        from agento.framework.harness.descriptor import ModelProviderDescriptor

        return ModelProviderDescriptor.from_declaration({
            "id": "p", "label": "P", "credential_required": True,
            "credential_scope": "s", "registration_modes": modes,
        })

    def test_oauth_provider_takes_no_flag(self):
        from agento.framework.cli.install import _registration_flag

        assert _registration_flag(self._provider(["interactive_oauth", "api_key"])) == ""

    def test_secret_only_provider_gets_its_flag(self):
        from agento.framework.cli.install import _registration_flag

        assert _registration_flag(
            self._provider(["api_key", "access_token"])
        ) == "--with-api-key"

    def test_access_token_only_provider_gets_that_flag(self):
        from agento.framework.cli.install import _registration_flag

        assert _registration_flag(
            self._provider(["access_token"])
        ) == "--with-access-token"

    def test_the_fixture_harness_secret_only_provider_is_driveable(self):
        from agento.framework.cli.install import _registration_flag
        from agento.framework.harness import parse_harness_declarations

        fixtures = REPO / "tests" / "fixtures" / "modules" / "fake_harness"
        (decl,) = parse_harness_declarations(fixtures / "di.json", "fake_harness")
        cloud = decl.descriptor.provider("fake_cloud")

        assert _registration_flag(cloud) == "--with-api-key"

    def test_wizard_appends_the_flag_for_a_secret_only_provider(self, tmp_path):
        """End-to-end through the wizard, with registration actually failing so we also
        prove a failed registration does not bind config."""
        from agento.framework.cli.install import _setup_agent_harness

        mod = tmp_path / "app" / "code" / "hermes"
        mod.mkdir(parents=True)
        (mod / "module.json").write_text(json.dumps({"name": "hermes", "version": "0.1"}))
        (mod / "di.json").write_text(json.dumps({"agent_harnesses": [{
            "id": "hermes", "label": "Hermes", "class": "src.adapter.A",
            "default_provider": "cloud",
            "providers": [{
                "id": "cloud", "label": "Cloud", "credential_required": True,
                "credential_scope": "hermes_cloud", "registration_modes": ["api_key"],
            }],
        }]}))

        def _select(prompt, options):
            return next(i for i, o in enumerate(options) if o.startswith("Hermes"))

        with (
            patch("agento.framework.cli.terminal.select", _select),
            patch("agento.framework.cli.install.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0)
            _setup_agent_harness(["docker", "compose"], tmp_path)

        argvs = [" ".join(c.args[0]) for c in run.call_args_list]
        assert any(
            "credential:register hermes_cloud default --with-api-key" in a for a in argvs
        ), argvs


class TestRunnerCallbacksAreInTheContract:
    """The consumer assigned ``runner.pid_callback`` / ``runner.session_id_callback``
    directly — a required surface that never appeared in the protocol. A structurally
    valid runner using ``__slots__`` crashed with AttributeError."""

    def test_observe_is_part_of_the_protocol(self):
        from agento.framework.harness.protocols import Runner as RunnerProto

        assert hasattr(RunnerProto, "observe")

    def test_consumer_configures_via_the_method_not_attribute_assignment(self):
        from agento.framework import consumer

        source = Path(consumer.__file__).read_text()
        assert "runner.observe(" in source
        assert "runner.pid_callback =" not in source
        assert "runner.session_id_callback =" not in source

    def test_a_slotted_runner_satisfies_the_contract(self):
        """The exact shape that used to crash."""

        class SlottedRunner:
            __slots__ = ("_on_session_id", "command_builder", "context")

            def __init__(self):
                self.context = None
                self.command_builder = None
                self._on_session_id = None

            def observe(self, *, on_pid=None, on_session_id=None) -> None:
                self._on_session_id = on_session_id

            def execute(self, request: RunRequest) -> RunResult:
                if self._on_session_id is not None:
                    self._on_session_id("slotted-session")
                return RunResult(raw_output="ok", session_id="slotted-session")

        runner = SlottedRunner()
        assert isinstance(runner, Runner)

        # Attribute assignment — what the consumer used to do — genuinely fails here.
        with pytest.raises(AttributeError):
            runner.pid_callback = lambda pid: None

        seen: list[str] = []
        runner.observe(on_pid=lambda pid: None, on_session_id=seen.append)
        runner.execute(RunRequest(prompt="x"))
        assert seen == ["slotted-session"]

    def test_shipped_runner_still_reports_both(self):
        register_builtin_harnesses()
        try:
            from tests.harness_fixtures import make_runner

            runner = make_runner("claude", credential=None, credential_required=False)
            pids: list[int] = []
            sids: list[str] = []
            runner.observe(on_pid=pids.append, on_session_id=sids.append)

            assert runner.pid_callback is not None
            assert runner.session_id_callback is not None
        finally:
            clear()

    def test_observe_tolerates_partial_configuration(self):
        """A caller that only cares about one hook must not clear the other."""
        register_builtin_harnesses()
        try:
            from tests.harness_fixtures import make_runner

            runner = make_runner("claude", credential=None, credential_required=False)
            runner.observe(on_session_id=lambda s: None)
            assert runner.pid_callback is None
            assert runner.session_id_callback is not None
        finally:
            clear()


class TestPypiExtensionsAreDiscoverable:
    """Compose bind-mounts PyPI extensions at ``/opt/agento-src/<ext>``, not into a venv.
    Discovery searched ``<root>/.venv/.../site-packages``, which does not exist in the
    container, and bootstrap scanned only core + ``/app/code`` — so a ``uv add``-ed harness
    was neither registered nor offered."""

    def _seed_extension(self, base: Path, name: str = "acme_harness") -> Path:
        ext = base / name
        ext.mkdir(parents=True)
        (ext / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
        (ext / "di.json").write_text(json.dumps({"agent_harnesses": [{
            "id": "acme", "label": "Acme", "class": "src.adapter.A",
            "default_provider": "cloud",
            "providers": [{"id": "cloud", "label": "Cloud", "credential_required": False}],
        }]}))
        return ext

    def test_mount_target_matches_what_compose_actually_writes(self):
        """Pin the coupling: if the compose mount path changes, this fails loudly."""
        from agento.framework.module_discovery import CONTAINER_EXTENSION_DIR

        provisioning = (
            REPO / "src" / "agento" / "framework" / "cli" / "_provisioning.py"
        ).read_text()
        assert f'mount_block("{CONTAINER_EXTENSION_DIR}")' in provisioning

    def test_extension_dirs_are_found_at_the_mount_point(self, tmp_path):
        from agento.framework import module_discovery

        self._seed_extension(tmp_path)
        with patch.object(
            module_discovery, "CONTAINER_EXTENSION_DIR", str(tmp_path),
        ):
            found = module_discovery._container_extension_dirs(str(tmp_path))
        assert [p.name for p in found] == ["acme_harness"]

    def test_the_framework_package_itself_is_excluded(self, tmp_path):
        """`/opt/agento-src/agento` is the framework, mounted alongside — and carries no
        module.json, so the manifest check filters it without a name special-case."""
        from agento.framework.module_discovery import _container_extension_dirs

        (tmp_path / "agento").mkdir()
        (tmp_path / "agento" / "__init__.py").write_text("")
        self._seed_extension(tmp_path)

        assert [p.name for p in _container_extension_dirs(str(tmp_path))] == [
            "acme_harness",
        ]

    def test_iter_module_dirs_includes_the_mounted_extension(self, tmp_path):
        from agento.framework import module_discovery

        self._seed_extension(tmp_path)
        with patch.object(
            module_discovery, "_container_extension_dirs",
            lambda root=None: [tmp_path / "acme_harness"],
        ):
            names = [d.name for d in module_discovery.iter_module_dirs(None)]
        assert "acme_harness" in names

    def test_a_mounted_harness_is_offered_by_resolve_options(self, tmp_path):
        from agento.framework import module_discovery
        from agento.framework.harness import resolve_options

        self._seed_extension(tmp_path)
        with patch.object(
            module_discovery, "_container_extension_dirs",
            lambda root=None: [tmp_path / "acme_harness"],
        ):
            values = {o["value"] for o in resolve_options("agent_harness_registry")}
        assert "acme" in values

    def test_bootstrap_scans_the_extension_mount(self):
        from agento.framework import bootstrap

        source = Path(bootstrap.__file__).read_text()
        assert "scan_all_modules(core_dir, user_dir)" in source
        # And no longer builds its own module set, which is how it drifted from setup.
        assert "scan_modules(core_dir) + scan_modules(user_dir)" not in source

    def test_extension_scan_is_shared_with_setup_and_validate(self):
        """Round 5: bootstrap alone was not enough — setup:upgrade and module:validate
        scanned a different set, so an extension could register while its SQL, data patches,
        cron and onboarding were skipped. All three now use one discovery path."""
        from agento.framework import bootstrap, setup

        for module in (bootstrap, setup):
            source = Path(module.__file__).read_text()
            assert "scan_all_modules(core_dir, user_dir)" in source, module.__name__

    def test_shared_scan_includes_the_extension_mount(self, tmp_path):
        from agento.framework import module_discovery

        self._seed_extension(tmp_path)
        with patch.object(
            module_discovery, "CONTAINER_EXTENSION_DIR", str(tmp_path),
        ):
            names = {m.name for m in module_discovery.scan_all_modules(
                str(tmp_path / "absent-core"), str(tmp_path / "absent-user"),
            )}
        assert names == {"acme_harness"}

    def test_shared_scan_does_not_let_an_extension_shadow_a_local_module(self, tmp_path):
        """Local app/code must win — an extension may not silently replace it."""
        from agento.framework import module_discovery

        local = tmp_path / "user" / "acme_harness"
        local.mkdir(parents=True)
        (local / "module.json").write_text(json.dumps(
            {"name": "acme_harness", "version": "9.9.9"}
        ))
        self._seed_extension(tmp_path / "ext")

        with patch.object(
            module_discovery, "CONTAINER_EXTENSION_DIR", str(tmp_path / "ext"),
        ):
            manifests = module_discovery.scan_all_modules(
                str(tmp_path / "absent-core"), str(tmp_path / "user"),
            )

        assert [m.version for m in manifests] == ["9.9.9"]

    def test_missing_mount_point_is_not_an_error(self, tmp_path):
        from agento.framework.module_discovery import _container_extension_dirs

        assert _container_extension_dirs(str(tmp_path / "absent")) == []


class TestNoStaleVocabularyLeft:
    def test_admin_dashboard_says_credentials(self):
        source = (
            REPO / "src" / "agento" / "framework" / "admin" / "screens" / "dashboard.py"
        ).read_text()
        assert '"Tokens"' not in source
        assert "No tokens registered" not in source

    def test_credential_list_help_says_credentials(self):
        source = (
            REPO / "src" / "agento" / "framework" / "cli" / "credential.py"
        ).read_text()
        assert "disabled tokens" not in source

    def test_decisions_log_matches_the_shipped_schema(self):
        """DECISIONS.md claimed no job.provider column existed — round 2 added one."""
        decisions = (REPO / "DECISIONS.md").read_text()
        assert "No `job.provider` column was added" not in decisions
        assert "031_job_provider.sql" in decisions
