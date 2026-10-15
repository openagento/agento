"""Tests for event data classes."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

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
    @pytest.mark.parametrize(("event_cls", "expected"), [
        pytest.param(JobSucceededEvent, {"job", "summary", "agent_type", "model", "elapsed_ms"}, id="job_succeeded"),
        pytest.param(JobFailedEvent, {"job", "error", "elapsed_ms"}, id="job_failed"),
        pytest.param(JobRetryingEvent, {"job", "error", "delay_seconds", "elapsed_ms"}, id="job_retrying"),
        pytest.param(JobDeadEvent, {"job", "error", "elapsed_ms"}, id="job_dead"),
        pytest.param(ModuleLoadedEvent, {"name", "path"}, id="module_loaded"),
        pytest.param(ModuleRegisterEvent, {"name", "path", "config"}, id="module_register"),
        pytest.param(ModuleReadyEvent, {"name", "path"}, id="module_ready"),
        pytest.param(ModuleShutdownEvent, {"name", "path"}, id="module_shutdown"),
        pytest.param(ConfigSavedEvent, {"path", "encrypted"}, id="config_saved"),
        pytest.param(SetupBeforeEvent, {"dry_run"}, id="setup_before"),
        pytest.param(SetupCompleteEvent, {"result", "dry_run"}, id="setup_complete"),
        pytest.param(MigrationAppliedEvent, {"version", "module", "path"}, id="migration_applied"),
        pytest.param(DataPatchAppliedEvent, {"name", "module"}, id="data_patch_applied"),
    ])
    def test_fields(self, event_cls, expected):
        assert {f.name for f in fields(event_cls)} == expected

    def test_consumer_started_has_no_fields(self):
        e = ConsumerStartedEvent()
        assert fields(e) == ()

    def test_consumer_stopping_has_no_fields(self):
        e = ConsumerStoppingEvent()
        assert fields(e) == ()

    def test_job_claimed_has_job_field(self):
        names = [f.name for f in fields(JobClaimedEvent)]
        assert "job" in names

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

    def test_config_saved_defaults(self):
        e = ConfigSavedEvent(path="jira/api_key")
        assert e.encrypted is False

