"""Per-job harness/model pinning — the pure resolver and its non-retryable failure."""
from __future__ import annotations

import pytest

from agento.framework.agent_view_runtime import (
    AgentViewRuntime,
    InvalidJobRuntime,
    resolve_job_runtime,
)
from agento.framework.retry_policy import evaluate as evaluate_retry

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


def _view(harness="claude", provider="anthropic", model="view-model"):
    return AgentViewRuntime(harness=harness, provider=provider, model=model)


def _other_harness(view_harness: str) -> tuple[str, str]:
    """A registered harness that is NOT the view's, with its default provider."""
    from agento.framework.harness import list_harnesses

    for registered in list_harnesses():
        if registered.descriptor.id != view_harness:
            return str(registered.descriptor.id), str(registered.descriptor.default_provider)
    pytest.skip("only one harness registered")


def test_nothing_pinned_uses_the_view():
    eff = resolve_job_runtime(_view(), pinned_harness=None, pinned_model=None)
    assert (eff.harness, eff.provider, eff.model) == ("claude", "anthropic", "view-model")


def test_model_pinned_alone_keeps_the_view_harness():
    eff = resolve_job_runtime(_view(), pinned_harness=None, pinned_model="pinned-model")
    assert (eff.harness, eff.provider, eff.model) == ("claude", "anthropic", "pinned-model")


def test_harness_pinned_to_the_view_harness_keeps_the_view_model():
    eff = resolve_job_runtime(_view(), pinned_harness="claude", pinned_model=None)
    assert (eff.harness, eff.provider, eff.model) == ("claude", "anthropic", "view-model")


def test_harness_pinned_elsewhere_drops_the_view_provider_and_model():
    other, default_provider = _other_harness("claude")
    eff = resolve_job_runtime(_view(), pinned_harness=other, pinned_model=None)
    assert eff.harness == other
    assert eff.provider == default_provider
    assert eff.model is None


def test_both_pinned_ignores_the_view_entirely():
    other, default_provider = _other_harness("claude")
    eff = resolve_job_runtime(_view(), pinned_harness=other, pinned_model="m")
    assert (eff.harness, eff.provider, eff.model) == (other, default_provider, "m")


def test_pinned_model_beats_the_consumer_model_override():
    eff = resolve_job_runtime(
        _view(), pinned_harness=None, pinned_model="pinned", model_override="cli",
    )
    assert eff.model == "pinned"


def test_model_override_beats_config():
    eff = resolve_job_runtime(
        _view(), pinned_harness=None, pinned_model=None, model_override="cli",
    )
    assert eff.model == "cli"


def test_unknown_pinned_harness_raises():
    with pytest.raises(InvalidJobRuntime, match="nope"):
        resolve_job_runtime(_view(), pinned_harness="nope", pinned_model=None)


def test_no_harness_at_all_raises():
    with pytest.raises(InvalidJobRuntime, match="agent_view/harness"):
        resolve_job_runtime(
            _view(harness=None, provider=None), pinned_harness=None, pinned_model=None,
        )


def test_invalid_job_runtime_is_not_retried():
    decision = evaluate_retry("InvalidJobRuntime", attempt=1, max_attempts=3)
    assert decision.should_retry is False


class TestTolerateViewPairError:
    """A job that pins a harness must not be blocked by the view's own bad pair."""

    def _patched(self):
        from unittest.mock import MagicMock, patch

        conn = MagicMock()
        return conn, patch(
            "agento.framework.agent_view_runtime._resolve_harness_and_provider",
            side_effect=ValueError("Harness 'claude' does not offer provider 'openai'"),
        )

    def test_raises_by_default(self):
        from agento.framework.agent_view_runtime import resolve_agent_view_runtime

        conn, patched = self._patched()
        with patched, pytest.raises(ValueError):
            resolve_agent_view_runtime(conn, None)

    def test_tolerated_leaves_harness_and_provider_unset(self):
        from agento.framework.agent_view_runtime import resolve_agent_view_runtime

        conn, patched = self._patched()
        with patched:
            runtime = resolve_agent_view_runtime(conn, None, tolerate_pair_error=True)
        assert runtime.harness is None
        assert runtime.provider is None
