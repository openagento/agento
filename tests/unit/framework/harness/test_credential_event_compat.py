"""Renaming five events can't silently break a third-party observer.

Every credential event is dispatched twice — under its new ``credential_*`` name and
under the deprecated ``token_*`` name — with BOTH payloads built from the same data, so
an observer bound to either name sees consistent values. Removal is tracked in ROADMAP.md.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agento.framework.events import (
    _CREDENTIAL_EVENT_ALIASES,
    CredentialAuthFailedEvent,
    CredentialAuthThrottledEvent,
    CredentialRefreshedEvent,
    CredentialRegisteredEvent,
    CredentialUsageLimitedEvent,
    dispatch_credential_event,
)


@pytest.fixture
def dispatched(monkeypatch):
    manager = MagicMock()
    monkeypatch.setattr(
        "agento.framework.event_manager.get_event_manager", lambda: manager,
    )

    def _get():
        return {name: payload for name, payload in
                (c.args for c in manager.dispatch.call_args_list)}

    return _get


class TestDualDispatch:
    def test_register_reaches_both_names(self, dispatched):
        dispatch_credential_event(
            "credential_register_after",
            CredentialRegisteredEvent(
                scope="codex", credential_id=7, label="l",
                credentials={"api_key": "sk-X"}, type="openai_api_key",
            ),
        )

        events = dispatched()
        assert set(events) == {"credential_register_after", "token_register_after"}

        new, legacy = events["credential_register_after"], events["token_register_after"]
        assert (new.scope, new.credential_id) == ("codex", 7)
        assert (legacy.agent_type, legacy.token_id) == ("codex", 7)
        # Same data, two shapes — an observer on either name must agree.
        assert legacy.credentials == new.credentials == {"api_key": "sk-X"}
        assert legacy.type == new.type == "openai_api_key"

    @pytest.mark.parametrize("new_name,legacy_name", sorted(
        (k, v[0]) for k, v in _CREDENTIAL_EVENT_ALIASES.items()
    ))
    def test_every_alias_is_wired(self, new_name, legacy_name, dispatched):
        payloads = {
            "credential_register_after": CredentialRegisteredEvent(
                scope="claude", credential_id=1, label="l", credentials={}, type="oauth",
            ),
            "credential_refresh_after": CredentialRefreshedEvent(
                scope="claude", credential_id=1, label="l", credentials={}, type="oauth",
            ),
            "credential_auth_failed_after": CredentialAuthFailedEvent(
                scope="claude", credential_id=1, error_msg="boom", job_id=3,
            ),
            "credential_usage_limited_after": CredentialUsageLimitedEvent(
                scope="claude", credential_id=1, error_msg="limit", job_id=3,
            ),
            "credential_auth_throttled_after": CredentialAuthThrottledEvent(
                scope="claude", credential_id=1, error_msg="401", job_id=3,
                throttled_until=None,
            ),
        }

        dispatch_credential_event(new_name, payloads[new_name])

        events = dispatched()
        assert set(events) == {new_name, legacy_name}
        assert events[legacy_name].agent_type == "claude"
        assert events[legacy_name].token_id == 1

    def test_an_unaliased_event_dispatches_once(self, dispatched):
        dispatch_credential_event(
            "credential_something_new_after",
            CredentialRegisteredEvent(
                scope="claude", credential_id=1, label="l", credentials={}, type="oauth",
            ),
        )
        assert set(dispatched()) == {"credential_something_new_after"}


class TestSecretsAreNotRepred:
    """Credential payloads land in logs via ``repr``, so the field is repr-suppressed."""

    @pytest.mark.parametrize("cls", [CredentialRegisteredEvent, CredentialRefreshedEvent])
    def test_credentials_field_is_hidden_from_repr(self, cls):
        event = cls(
            scope="claude", credential_id=1, label="l",
            credentials={"api_key": "sk-SECRET"}, type="oauth",
        )
        assert "sk-SECRET" not in repr(event)

    def test_legacy_payload_hides_it_too(self, dispatched):
        dispatch_credential_event(
            "credential_register_after",
            CredentialRegisteredEvent(
                scope="claude", credential_id=1, label="l",
                credentials={"api_key": "sk-SECRET"}, type="oauth",
            ),
        )
        legacy = dispatched()["token_register_after"]
        assert "sk-SECRET" not in repr(legacy)
        # Still readable by an observer that actually needs it.
        assert legacy.credentials == {"api_key": "sk-SECRET"}
