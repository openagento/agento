"""Tests for event data classes."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from agento.framework.events import (
    ConfigSavedEvent,
    ConsumerStartedEvent,
    ConsumerStoppingEvent,
    DataPatchAppliedEvent,
    JobClaimedEvent,
    JobDeadEvent,
    JobFailedEvent,
    JobRetryingEvent,
    JobSucceededEvent,
    MigrationAppliedEvent,
    ModuleLoadedEvent,
    ModuleReadyEvent,
    ModuleRegisterEvent,
    ModuleShutdownEvent,
    SetupBeforeEvent,
    SetupCompleteEvent,
)


class TestEventDataClasses:
    def test_consumer_started_has_no_fields(self):
        e = ConsumerStartedEvent()
        assert fields(e) == ()

    def test_consumer_stopping_has_no_fields(self):
        e = ConsumerStoppingEvent()
        assert fields(e) == ()

    def test_job_claimed_has_job_field(self):
        names = [f.name for f in fields(JobClaimedEvent)]
        assert "job" in names

    def test_job_succeeded_fields(self):
        names = [f.name for f in fields(JobSucceededEvent)]
        assert set(names) == {"job", "summary", "agent_type", "model", "elapsed_ms"}

    def test_job_failed_fields(self):
        names = [f.name for f in fields(JobFailedEvent)]
        assert set(names) == {"job", "error", "elapsed_ms"}

    def test_job_retrying_fields(self):
        names = [f.name for f in fields(JobRetryingEvent)]
        assert set(names) == {"job", "error", "delay_seconds", "elapsed_ms"}

    def test_job_dead_fields(self):
        names = [f.name for f in fields(JobDeadEvent)]
        assert set(names) == {"job", "error", "elapsed_ms"}

    def test_module_loaded_fields(self):
        names = [f.name for f in fields(ModuleLoadedEvent)]
        assert set(names) == {"name", "path"}

    def test_module_register_fields(self):
        names = [f.name for f in fields(ModuleRegisterEvent)]
        assert set(names) == {"name", "path", "config"}

    def test_module_ready_fields(self):
        names = [f.name for f in fields(ModuleReadyEvent)]
        assert set(names) == {"name", "path"}

    def test_module_shutdown_fields(self):
        names = [f.name for f in fields(ModuleShutdownEvent)]
        assert set(names) == {"name", "path"}

    def test_events_are_mutable(self):
        e = ModuleLoadedEvent(name="test", path=Path("/tmp"))
        e.name = "changed"
        assert e.name == "changed"

    def test_job_succeeded_defaults(self):
        from unittest.mock import MagicMock
        job = MagicMock()
        e = JobSucceededEvent(job=job)
        assert e.summary is None
        assert e.elapsed_ms == 0

    # --- Phase 8: Config & setup lifecycle events ---

    def test_config_saved_fields(self):
        names = [f.name for f in fields(ConfigSavedEvent)]
        assert set(names) == {"path", "encrypted"}

    def test_config_saved_defaults(self):
        e = ConfigSavedEvent(path="jira/api_key")
        assert e.encrypted is False

    def test_setup_before_fields(self):
        names = [f.name for f in fields(SetupBeforeEvent)]
        assert set(names) == {"dry_run"}

    def test_setup_complete_fields(self):
        names = [f.name for f in fields(SetupCompleteEvent)]
        assert set(names) == {"result", "dry_run"}

    def test_migration_applied_fields(self):
        names = [f.name for f in fields(MigrationAppliedEvent)]
        assert set(names) == {"version", "module", "path"}

    def test_data_patch_applied_fields(self):
        names = [f.name for f in fields(DataPatchAppliedEvent)]
        assert set(names) == {"name", "module"}

