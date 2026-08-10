"""Unit: a transient auth failure THROTTLES the offending token (cooldown, not
poison) and sets ``retry_with_other_token`` when the pool has an alternative — unless the
credential has nothing to rotate, in which case the rejection is real and it is poisoned."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agento.framework.agent_manager.errors import TransientAuthError
from agento.framework.consumer import Consumer


def _consumer():
    return Consumer(MagicMock(), MagicMock(), logging.getLogger("test"))


def _job():
    return SimpleNamespace(id=4242, agent_view_id=2)


def _token(token_id: int = 50, *, rotatable: bool = True):
    """A resolved token. ``rotatable`` mirrors the framework-owned flat ``refresh_token``
    field — the only thing that decides throttle-vs-poison here (never a provider-specific
    ``type == 'oauth'`` literal)."""
    creds = {"subscription_key": "sk"} | ({"refresh_token": "R0"} if rotatable else {})
    return SimpleNamespace(id=token_id, credentials=creds)


def _call(healthy: int, exc: TransientAuthError, *, rotatable: bool = True):
    """Drive _handle_transient_auth with a stubbed DB layer; return the throttle
    call args recorded by the patched token_store function."""
    with (
        patch("agento.framework.consumer.get_connection"),
        patch("agento.framework.consumer.throttle_credential") as throttle,
        patch("agento.framework.consumer.mark_credential_error") as poison,
        patch(
            "agento.framework.consumer.count_credentials_for_scope",
            return_value=(3, healthy),
        ),
        # dispatch_credential_event resolves the manager itself, so patch it at the
        # source rather than through the consumer's own import.
        patch("agento.framework.event_manager.get_event_manager") as em,
    ):
        _consumer()._handle_transient_auth(
            _job(), _token(rotatable=rotatable), "claude", exc
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


def test_transient_auth_dispatches_credential_auth_throttled_after():
    _throttle, _poison, dispatch = _call(
        healthy=1, exc=TransientAuthError("401 OAuth access token has been revoked")
    )
    names = [c.args[0] for c in dispatch.call_args_list]
    # Dual dispatch: new name + deprecated alias.
    assert "credential_auth_throttled_after" in names
    assert "token_auth_throttled_after" in names
    # Must NOT masquerade as a poison — workspace_build binds an observer to that one.
    assert "credential_auth_failed_after" not in names
    assert "token_auth_failed_after" not in names


def test_the_incident_401_now_lands_here_instead_of_poisoning_the_subscription():
    """The exact k3-agento message. It used to match AUTH_ERROR_PHRASES (rule 1 shadowing
    rule 3) and quarantine a healthy subscription; a replayed single-use refresh token is
    transient by construction, so it belongs on this path."""
    exc = TransientAuthError(
        "Claude CLI error: Failed to authenticate. API Error: "
        "401 Invalid authentication credentials"
    )
    throttle, poison, dispatch = _call(healthy=1, exc=exc)
    assert throttle.called
    assert not poison.called
    names = [c.args[0] for c in dispatch.call_args_list]
    assert "token_auth_throttled_after" in names
    assert "token_auth_failed_after" not in names


def test_a_non_rotatable_credential_is_poisoned_not_throttled_forever():
    """An anthropic_api_key row has no stale copy to blame, so the same message means the
    credential really is dead — throttling it forever would trade one silent failure for
    another. It is still 'auto' provenance, so it self-heals if it ever works again."""
    exc = TransientAuthError("Failed to authenticate. API Error: 401 Invalid authentication credentials")
    throttle, poison, dispatch = _call(healthy=1, exc=exc, rotatable=False)
    assert not throttle.called
    assert poison.called
    assert poison.call_args.kwargs["source"] == "auto"
    names = [c.args[0] for c in dispatch.call_args_list]
    assert "token_auth_failed_after" in names
    assert "token_auth_throttled_after" not in names
