"""Unit: a usage/session limit THROTTLES the offending token (cooldown, not poison),
sets ``retry_with_other_token`` when the pool still has a healthy alternative, and — when
the WHOLE pool is throttled — sets ``pool_retry_at`` so the job waits for quota instead of
dead-lettering (AG-46)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agento.framework.agent_manager.errors import UsageLimitError
from agento.framework.consumer import Consumer


def _consumer():
    return Consumer(MagicMock(), MagicMock(), logging.getLogger("test"))


def _job():
    return SimpleNamespace(id=4242, agent_view_id=2)


def _token(token_id: int = 50):
    return SimpleNamespace(id=token_id, credentials={"subscription_key": "sk"})


def _call(healthy: int, exc: UsageLimitError, *, earliest=None):
    """Drive _handle_usage_limit with a stubbed DB layer; return the throttle call args."""
    with (
        patch("agento.framework.consumer.get_connection"),
        patch("agento.framework.consumer.throttle_credential") as throttle,
        patch(
            "agento.framework.consumer.count_credentials_for_scope",
            return_value=(3, healthy),
        ),
        patch(
            "agento.framework.consumer.earliest_throttle_reset_for_scope",
            return_value=earliest,
        ),
        patch("agento.framework.event_manager.get_event_manager") as em,
    ):
        _consumer()._handle_usage_limit(_job(), _token(), "claude", exc)
        return throttle, em.return_value.dispatch


def test_usage_limit_throttles_the_token():
    throttle, _dispatch = _call(healthy=1, exc=UsageLimitError("usage limit reached"))
    assert throttle.called


def test_sets_failover_flag_when_a_healthy_token_remains():
    exc = UsageLimitError("usage limit reached")
    _call(healthy=2, exc=exc)
    assert exc.retry_with_other_token is True
    # A healthy token is available now — no need to schedule a pool-recovery wait.
    assert exc.pool_retry_at is None


def test_pool_exhausted_sets_pool_retry_at_to_earliest_reset():
    """AG-46: whole pool throttled -> wait for the earliest reset, don't dead-letter."""
    earliest = datetime(2026, 8, 27, 15, 0, 0)
    exc = UsageLimitError("usage limit reached")
    _call(healthy=0, exc=exc, earliest=earliest)
    assert exc.retry_with_other_token is False
    assert exc.pool_retry_at == earliest


def test_pool_retry_at_falls_back_to_own_reset_when_query_finds_nothing():
    """If the pool query returns nothing, use this credential's own throttle reset."""
    reset_at = datetime(2026, 8, 27, 16, 30, 0)
    exc = UsageLimitError("usage limit reached", reset_at=reset_at)
    _call(healthy=0, exc=exc, earliest=None)
    assert exc.pool_retry_at == reset_at


def test_pool_retry_at_defaults_when_no_reset_and_no_pool_info():
    """No parseable reset and an empty pool query still yields a future wait time."""
    exc = UsageLimitError("usage limit reached")  # reset_at=None
    _call(healthy=0, exc=exc, earliest=None)
    assert exc.pool_retry_at is not None
    assert exc.pool_retry_at > datetime.now(UTC).replace(tzinfo=None)
