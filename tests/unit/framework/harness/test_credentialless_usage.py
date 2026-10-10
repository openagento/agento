"""A provider that needs no credential must still be observable.

``usage_log.credential_id`` is nullable so a credential-less run is recorded and
attributed by ``(harness, provider)`` instead of vanishing. An early ``return`` here
(the shape the first draft had) would have made exactly that class of run invisible.
"""
from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from agento.framework.harness import HarnessRunContext
from agento.framework.runner.client import RemoteRunner
from tests.harness_fixtures import make_runner


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def _record_usage(conn, **kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(
        "agento.framework.agent_manager.usage_store.record_usage", _record_usage,
    )
    return calls


def _runner(monkeypatch, harness="claude", *, credential):
    """The worker records usage (WS5): the runner holds no database, it only sends the
    ``usage`` event that ``RemoteRunner`` writes."""
    local = make_runner(harness, credential=credential, credential_required=credential is not None,
                        model="opus-4")
    monkeypatch.setattr("agento.framework.db.pooled", lambda *a, **k: nullcontext(MagicMock()))
    return RemoteRunner(harness, local.context, logger=MagicMock())


def _result(**kw) -> dict:
    return {"input_tokens": 100, "output_tokens": 50, "duration_ms": 1200, "model": "opus-4", **kw}


class TestCredentiallessUsage:
    def test_run_without_a_credential_is_still_recorded(self, monkeypatch, recorder):
        runner = _runner(monkeypatch, credential=None)

        runner._record_usage(_result())

        assert len(recorder) == 1
        assert recorder[0]["credential_id"] is None
        assert recorder[0]["harness"] == "claude"
        assert recorder[0]["provider"] == "anthropic"
        assert recorder[0]["tokens_used"] == 150

    def test_run_with_a_credential_attributes_it(self, monkeypatch, recorder):
        from tests.unit.agent_manager.conftest import make_token

        credential = make_token(id=42, label="a", credentials={"api_key": "x"})
        runner = _runner(monkeypatch, credential=credential)

        runner._record_usage(_result())

        assert recorder[0]["credential_id"] == 42

    def test_recording_failure_never_breaks_the_run(self, monkeypatch):
        runner = _runner(monkeypatch, credential=None)
        monkeypatch.setattr(
            "agento.framework.agent_manager.usage_store.record_usage",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
        )

        runner._record_usage(_result())  # must not raise

    def test_harness_and_provider_travel_with_the_context(self, monkeypatch, recorder):
        """Attribution comes from the run context, not from the parsed result — a
        harness whose output omits them is still attributable."""
        runner = _runner(monkeypatch, "codex", credential=None)

        runner._record_usage(_result(input_tokens=None, output_tokens=None, duration_ms=None,
                                     model=None))

        assert (recorder[0]["harness"], recorder[0]["provider"]) == ("codex", "openai")


class TestHeadlessCredentialGuard:
    def test_missing_required_credential_is_refused_before_spawning(self):
        runner = make_runner("claude", credential=None, credential_required=True)
        runner._execute_process = MagicMock()

        from agento.framework.harness import RunRequest

        with pytest.raises(RuntimeError, match="No healthy credential"):
            runner.execute(RunRequest(prompt="hi"))

        runner._execute_process.assert_not_called()

    def test_credential_less_provider_needs_no_credential(self, monkeypatch, recorder):
        """The interactive `/login` case: nothing to claim, nothing to refuse."""
        ctx = HarnessRunContext(
            harness="claude", provider="anthropic", credential_required=False,
        )
        assert ctx.credential is None
        assert ctx.credential_required is False
