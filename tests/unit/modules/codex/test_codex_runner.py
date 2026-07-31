"""Unit: codex NDJSON error classification — permanent auth vs transient credential
rejection. Both are detected ONLY from a structured ``turn.failed.error``, never from
raw stdout."""
from __future__ import annotations

import json

import pytest

from agento.framework.agent_manager.errors import AuthenticationError, TransientAuthError
from agento.modules.codex.src.runner import TokenCodexRunner


def _turn_failed(message: str) -> str:
    return json.dumps({"type": "turn.failed", "error": {"message": message}}) + "\n"


def _parse(raw: str):
    return TokenCodexRunner._parse_output(object.__new__(TokenCodexRunner), raw)


def test_codex_revoked_token_raises_transient_auth_error():
    with pytest.raises(TransientAuthError):
        _parse(_turn_failed("401 the OAuth access token has been revoked"))


def test_codex_revoked_is_not_a_permanent_auth_error():
    with pytest.raises(TransientAuthError) as exc:
        _parse(_turn_failed("access token has been revoked"))
    assert not isinstance(exc.value, AuthenticationError)


def test_codex_existing_permanent_auth_phrases_are_unchanged():
    # Scope decision: codex's known auth phrases keep poisoning the token.
    for msg in ("401 Unauthorized", "invalid_api_key", "please sign in", "not authenticated"):
        with pytest.raises(AuthenticationError):
            _parse(_turn_failed(msg))


def test_codex_transient_only_matches_structured_turn_failed():
    # Anti-false-positive discipline: raw stdout mentioning "revoked" must not classify.
    raw = json.dumps({"type": "item.completed", "text": "the coupon has been revoked"}) + "\n"
    assert _parse(raw) is not None


@pytest.mark.parametrize("msg", [
    # Revocation wording with no credential context is an authorization failure inside
    # the run, not a rejected pool credential — same two-signal discipline as Claude.
    "workspace access has been revoked",
    "permission has been revoked",
    "your access to the repository has been revoked",
])
def test_codex_revoked_without_credential_context_is_not_transient(msg):
    assert _parse(_turn_failed(msg)) is not None
