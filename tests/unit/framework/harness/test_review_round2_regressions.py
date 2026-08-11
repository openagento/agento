"""Regression tests for impl review round 2.

The theme of this round: metadata that was *computed* correctly but then thrown away, so
a later step silently substituted a default.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import clear, parse_harness_declarations, register_harness
from agento.framework.job_models import AgentType, Job, JobStatus
from agento.framework.module_loader import import_class
from tests.harness_fixtures import register_builtin_harnesses

from .test_review_round1_regressions import FIXTURES


def _job(**overrides) -> Job:
    defaults = dict(
        id=42, schedule_id=None, type=AgentType.CRON, source="jira",
        agent_view_id=None, priority=50, reference_id="AI-1",
        agent_type="claude", provider="anthropic", model="claude-opus-4-7",
        input_tokens=1, output_tokens=1, prompt="p", output=None, context=None,
        idempotency_key="k", status=JobStatus.SUCCESS, attempt=1, max_attempts=3,
        scheduled_after=None, started_at=None, finished_at=None,
        result_summary=None, error_message=None, error_class=None, pid=None,
        session_id=None, created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestJobRecordsItsProvider:
    """`job.agent_type` holds the harness; without a provider column the job could not say
    which vendor served the run, so replay substituted the harness DEFAULT — replaying a
    non-default-provider run on the wrong provider."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        clear()
        module_dir = FIXTURES / "fake_harness"
        for decl in parse_harness_declarations(module_dir / "di.json", "fake_harness"):
            register_harness(decl.descriptor, import_class(module_dir, decl.class_path)())
        yield
        clear()

    def test_replay_uses_the_recorded_non_default_provider(self):
        from agento.framework.replay import build_replay_command

        # 'fake_cloud' is NOT the harness default ('fake_local').
        job = _job(agent_type="fake", provider="fake_cloud", model="m1")

        replay = build_replay_command(job)

        assert replay.provider == "fake_cloud"

    def test_pre_migration_row_falls_back_to_the_default(self):
        """Rows written before `job.provider` existed genuinely don't know it."""
        from agento.framework.replay import build_replay_command

        replay = build_replay_command(_job(agent_type="fake", provider=None))

        assert replay.provider == "fake_local"

    def test_explicit_override_wins(self):
        from agento.framework.replay import build_replay_command

        replay = build_replay_command(
            _job(agent_type="fake", provider="fake_local"),
            provider_override="fake_cloud",
        )
        assert replay.provider == "fake_cloud"

    def test_override_the_harness_does_not_offer_raises(self):
        from agento.framework.replay import build_replay_command

        with pytest.raises(ValueError, match="does not offer provider"):
            build_replay_command(_job(agent_type="fake"), provider_override="nope")

    def test_stale_recorded_provider_falls_back_rather_than_failing(self):
        """A provider the harness no longer offers (module downgraded) must not hard-fail
        a replay — the default is a usable answer."""
        from agento.framework.replay import build_replay_command

        replay = build_replay_command(_job(agent_type="fake", provider="retired"))

        assert replay.provider == "fake_local"

    def test_job_from_row_reads_the_column(self):
        row = {
            "id": 1, "schedule_id": None, "type": "cron", "source": "jira",
            "agent_view_id": None, "priority": 50, "reference_id": "AI-1",
            "agent_type": "fake", "provider": "fake_cloud", "model": None,
            "input_tokens": None, "output_tokens": None, "prompt": None, "output": None,
            "context": None, "idempotency_key": "k", "status": "SUCCESS", "attempt": 1,
            "max_attempts": 3, "scheduled_after": None, "started_at": None,
            "finished_at": None, "result_summary": None, "error_message": None,
            "error_class": None, "pid": None, "session_id": None,
            "created_at": None, "updated_at": None,
        }
        assert Job.from_row(row).provider == "fake_cloud"

    def test_from_row_tolerates_a_missing_column(self):
        """Reading a row mid-migration (column not added yet) must not KeyError."""
        row = {
            "id": 1, "schedule_id": None, "type": "cron", "source": "jira",
            "agent_view_id": None, "priority": 50, "reference_id": "AI-1",
            "agent_type": "fake", "model": None,
            "input_tokens": None, "output_tokens": None, "prompt": None, "output": None,
            "context": None, "idempotency_key": "k", "status": "SUCCESS", "attempt": 1,
            "max_attempts": 3, "scheduled_after": None, "started_at": None,
            "finished_at": None, "result_summary": None, "error_message": None,
            "error_class": None, "pid": None, "session_id": None,
            "created_at": None, "updated_at": None,
        }
        assert "provider" not in row
        assert Job.from_row(row).provider is None


class TestSchemaShipsBothPaths:
    """A column added only to the migration breaks fresh installs, and one added only to
    init breaks upgrades."""

    def test_migration_exists_and_is_seeded_in_init(self):
        from pathlib import Path

        sql_dir = Path("src/agento/framework/sql")
        migration = sql_dir / "031_job_provider.sql"
        assert migration.exists()
        assert "ADD COLUMN provider" in migration.read_text()

        init = (sql_dir / "init" / "000_init.sql").read_text()
        # Fresh installs must NOT re-run the migration on first setup:upgrade.
        assert "('031_job_provider')" in init

    def test_init_job_table_declares_the_column(self):
        from pathlib import Path

        init = (Path("src/agento/framework/sql/init/000_init.sql")).read_text()
        job_block = init[init.index("CREATE TABLE IF NOT EXISTS job"):]
        job_block = job_block[: job_block.index(";")]
        assert "provider" in job_block


class TestDependentSelectValidation:
    """`config:set agent_view/provider` validated against the union of EVERY harness's
    providers, so `(claude, openai)` was accepted and only failed at runtime."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def _conn_with(self, rows: dict):
        """A conn whose scoped-config lookups return ``rows`` (path -> value)."""
        conn = MagicMock()
        return conn, rows

    def test_provider_is_narrowed_to_the_effective_harness(self, monkeypatch, capsys):
        from agento.framework.cli.config import _validate_config_value
        from agento.framework.scoped_config import Scope

        # The view's harness is codex (set at the agent_view scope).
        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides",
            lambda _c, scope, sid: (
                {"agent_view/harness": ("codex", False)}
                if scope == Scope.AGENT_VIEW else {}
            ),
        )

        ok = _validate_config_value(
            "agent_view/provider", "anthropic",
            conn=MagicMock(), scope=Scope.AGENT_VIEW, scope_id=7,
        )

        assert ok is False
        out = capsys.readouterr().out
        assert "Invalid value 'anthropic'" in out
        assert "openai" in out  # tells the operator what codex DOES offer

    def test_the_harnesss_own_provider_is_accepted(self, monkeypatch):
        from agento.framework.cli.config import _validate_config_value
        from agento.framework.scoped_config import Scope

        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides",
            lambda _c, scope, sid: (
                {"agent_view/harness": ("codex", False)}
                if scope == Scope.AGENT_VIEW else {}
            ),
        )

        assert _validate_config_value(
            "agent_view/provider", "openai",
            conn=MagicMock(), scope=Scope.AGENT_VIEW, scope_id=7,
        ) is True

    def test_inherited_harness_still_narrows(self, monkeypatch, capsys):
        """The harness set at the DEFAULT scope must narrow an agent_view-scoped provider —
        raw DB rows at the target scope alone would miss it."""
        from agento.framework.cli.config import _validate_config_value
        from agento.framework.scoped_config import Scope

        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides",
            lambda _c, scope, sid: (
                {"agent_view/harness": ("codex", False)}
                if scope == Scope.DEFAULT else {}
            ),
        )

        assert _validate_config_value(
            "agent_view/provider", "anthropic",
            conn=MagicMock(), scope=Scope.AGENT_VIEW, scope_id=7,
        ) is False

    def test_env_harness_narrows_too(self, monkeypatch):
        from agento.framework.cli.config import _validate_config_value
        from agento.framework.scoped_config import Scope

        monkeypatch.setenv("CONFIG__AGENT_VIEW__HARNESS", "codex")
        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides",
            lambda *_a: {},
        )

        assert _validate_config_value(
            "agent_view/provider", "anthropic",
            conn=MagicMock(), scope=Scope.AGENT_VIEW, scope_id=7,
        ) is False


class TestSwitchingHarnessResetsProvider:
    """The actual user-visible failure: flipping a claude view to codex left provider
    `anthropic`, and the next job died in resolution."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def test_incompatible_provider_is_reset_to_the_new_default(self, monkeypatch):
        from agento.framework.config_dependents import reset_dependents
        from agento.framework.scoped_config import Scope

        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides",
            lambda _c, scope, sid: (
                {"agent_view/provider": ("anthropic", False)}
                if scope == Scope.AGENT_VIEW else {}
            ),
        )
        written = []
        monkeypatch.setattr(
            "agento.framework.core_config.config_set_auto_encrypt",
            lambda conn, path, value, **kw: written.append((path, value)) or False,
        )

        changed = reset_dependents(
            MagicMock(), "agent_view/harness", "codex",
            scope=Scope.AGENT_VIEW, scope_id=7,
        )

        assert changed == [("agent_view/provider", "openai")]
        assert written == [("agent_view/provider", "openai")]

    def test_a_compatible_provider_is_left_alone(self, monkeypatch):
        from agento.framework.config_dependents import reset_dependents
        from agento.framework.scoped_config import Scope

        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides",
            lambda _c, scope, sid: (
                {"agent_view/provider": ("openai", False)}
                if scope == Scope.AGENT_VIEW else {}
            ),
        )
        monkeypatch.setattr(
            "agento.framework.core_config.config_set_auto_encrypt",
            lambda *a, **kw: pytest.fail("must not rewrite a valid provider"),
        )

        assert reset_dependents(
            MagicMock(), "agent_view/harness", "codex",
            scope=Scope.AGENT_VIEW, scope_id=7,
        ) == []

    def test_absent_provider_is_seeded_with_the_default(self, monkeypatch):
        """An unset dependent is as broken as an incompatible one once the parent moves."""
        from agento.framework.config_dependents import reset_dependents
        from agento.framework.scoped_config import Scope

        monkeypatch.setattr(
            "agento.framework.scoped_config.load_scoped_db_overrides", lambda *_a: {},
        )
        monkeypatch.setattr(
            "agento.framework.core_config.config_set_auto_encrypt",
            lambda *a, **kw: False,
        )

        changed = reset_dependents(
            MagicMock(), "agent_view/harness", "codex",
            scope=Scope.AGENT_VIEW, scope_id=7,
        )
        assert changed == [("agent_view/provider", "openai")]

    def test_a_non_parent_path_resets_nothing(self, monkeypatch):
        from agento.framework.config_dependents import reset_dependents
        from agento.framework.scoped_config import Scope

        assert reset_dependents(
            MagicMock(), "agent_view/model", "opus",
            scope=Scope.AGENT_VIEW, scope_id=7,
        ) == []


class TestPublicContractKeepsTheProtocol:
    def test_runner_protocol_is_exported(self):
        """It was exported pre-0.15; replacing it with the concrete class broke importers
        and contradicts "dependencies through protocols"."""
        from agento.framework import contracts

        assert "Runner" in contracts.__all__
        assert contracts.Runner is not contracts.SubprocessRunner

    def test_concrete_runner_stays_available_too(self):
        from agento.framework import contracts

        assert "SubprocessRunner" in contracts.__all__

    def test_shipped_adapters_honour_the_declared_bool_return(self):
        """The protocol documents `-> bool`; both adapters used to return None."""
        import inspect

        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
        from agento.modules.codex.src.config import CodexWorkspaceAdapter

        for cls in (ClaudeWorkspaceAdapter, CodexWorkspaceAdapter):
            sig = inspect.signature(cls.capture_refreshed_credentials)
            # `from __future__ import annotations` keeps annotations as strings.
            assert sig.return_annotation in (bool, "bool"), cls.__name__

    def test_capture_returns_false_when_there_is_nothing_to_capture(self, tmp_path):
        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter
        from tests.unit.agent_manager.conftest import make_token

        # An api-key credential carries no refresh token the CLI could rotate.
        credential = make_token(id=1, label="l", type="anthropic_api_key",
                                credentials={"api_key": "sk-X"})

        assert ClaudeWorkspaceAdapter().capture_refreshed_credentials(
            tmp_path, credential, MagicMock(),
        ) is False


class TestCredentialVocabulary:
    def test_errors_expose_credential_id(self):
        """`token_id` on a credential-domain exception was the last confusing holdover."""
        from agento.framework.agent_manager.errors import (
            AuthenticationError,
            UsageLimitError,
        )

        assert AuthenticationError("x", credential_id=7).credential_id == 7
        assert UsageLimitError("y", credential_id=9).credential_id == 9

    def test_no_token_id_attribute_remains(self):
        from agento.framework.agent_manager.errors import AuthenticationError

        assert not hasattr(AuthenticationError("x", credential_id=1), "token_id")
