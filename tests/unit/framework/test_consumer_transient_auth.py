"""Unit: a transient auth failure THROTTLES the offending token (cooldown, not
poison) and sets ``retry_with_other_token`` when the pool has an alternative."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agento.framework.agent_manager.errors import TransientAuthError
from agento.framework.agent_manager.models import AgentProvider
from agento.framework.consumer import Consumer


def _consumer():
    return Consumer(MagicMock(), MagicMock(), logging.getLogger("test"))


def _job():
    return SimpleNamespace(id=4242, agent_view_id=2)


def _call(healthy: int, exc: TransientAuthError):
    """Drive _handle_transient_auth with a stubbed DB layer; return the throttle
    call args recorded by the patched token_store function."""
    with (
        patch("agento.framework.consumer.get_connection"),
        patch("agento.framework.consumer.throttle_token") as throttle,
        patch("agento.framework.consumer.mark_token_error") as poison,
        patch(
            "agento.framework.consumer.count_tokens_for_provider",
            return_value=(3, healthy),
        ),
        patch("agento.framework.consumer.get_event_manager") as em,
    ):
        _consumer()._handle_transient_auth(
            _job(), SimpleNamespace(id=50), AgentProvider.CLAUDE, exc
        )
        return throttle, poison, em.return_value.dispatch


def test_transient_auth_throttles_and_never_poisons():
    throttle, poison, _dispatch = _call(
        healthy=1, exc=TransientAuthError("401 OAuth access token has been revoked")
    )
    assert throttle.called
    # The bug being fixed: id=50 must NOT be flipped to status='error'.
    assert not poison.called


def test_transient_auth_throttle_window_is_in_the_future_and_naive_utc():
    throttle, _poison, _dispatch = _call(
        healthy=1, exc=TransientAuthError("401 OAuth access token has been revoked")
    )
    until = throttle.call_args.args[2]
    assert isinstance(until, datetime)
    assert until.tzinfo is None, "throttled_until must be naive UTC (DB column is UTC)"
    assert until > datetime.now(UTC).replace(tzinfo=None)


def test_transient_auth_sets_failover_flag_when_a_healthy_token_remains():
    exc = TransientAuthError("401 OAuth access token has been revoked")
    _call(healthy=2, exc=exc)
    assert exc.retry_with_other_token is True


def test_transient_auth_leaves_failover_flag_false_when_pool_exhausted():
    exc = TransientAuthError("401 OAuth access token has been revoked")
    _call(healthy=0, exc=exc)
    assert exc.retry_with_other_token is False


def test_transient_auth_attributes_failure_to_resolved_token_when_token_id_absent():
    throttle, _poison, _dispatch = _call(
        healthy=1, exc=TransientAuthError("401 OAuth access token has been revoked")
    )
    assert throttle.call_args.args[1] == 50


def test_transient_auth_dispatches_token_auth_throttled_after():
    _throttle, _poison, dispatch = _call(
        healthy=1, exc=TransientAuthError("401 OAuth access token has been revoked")
    )
    names = [c.args[0] for c in dispatch.call_args_list]
    assert "token_auth_throttled_after" in names
    # Must NOT masquerade as a poison — workspace_build binds an observer to that one.
    assert "token_auth_failed_after" not in names
