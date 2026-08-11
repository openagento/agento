from __future__ import annotations

import json
from datetime import datetime

import pytest

from agento.framework.agent_manager.models import (
    CredentialRecord,
    CredentialStatus,
    UsageSummary,
)


class _FakeEncryptor:
    def encrypt(self, plaintext: str) -> str:
        return f"aes256:iv:{plaintext}"

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.split(":", 2)[-1]


@pytest.fixture(autouse=True)
def _fake_encryptor(monkeypatch):
    from agento.framework import encryptor as enc
    monkeypatch.setattr(enc, "_instance", _FakeEncryptor())
    yield


_CREDS = {"subscription_key": "sk-test"}
_CIPHERTEXT = f"aes256:iv:{json.dumps(_CREDS)}"


class TestScopeColumnFallback:
    """``scope`` is the new key; ``agent_type`` is dual-written for one release.

    A row written by an older build (or read mid-migration) has only ``agent_type``, so
    ``from_row`` falls back to it rather than raising.
    """

    def test_scope_column_wins_when_present(self):
        row = _row(agent_type="claude", scope="claude")
        assert CredentialRecord.from_row(row).scope == "claude"

    def test_falls_back_to_agent_type_when_scope_is_absent(self):
        row = _row(agent_type="codex")
        row.pop("scope", None)
        assert CredentialRecord.from_row(row).scope == "codex"

    def test_falls_back_when_scope_is_null(self):
        """The migration adds the column NULL before backfilling it."""
        row = _row(agent_type="codex", scope=None)
        assert CredentialRecord.from_row(row).scope == "codex"


def _row(**overrides) -> dict:
    row = {
        "id": 1,
        "agent_type": "claude",
        "label": "prod-1",
        "credentials": _CIPHERTEXT,
        "token_limit": 100000,
        "enabled": 1,
        "status": "ok",
        "error_msg": None,
        "expires_at": None,
        "used_at": None,
        "created_at": datetime(2025, 1, 1),
        "updated_at": datetime(2025, 1, 1),
    }
    row.update(overrides)
    return row


class TestCredentialStatus:
    def test_values(self):
        assert CredentialStatus.OK.value == "ok"
        assert CredentialStatus.ERROR.value == "error"


class TestCredentialRecord:
    def test_from_row(self):
        row = {
            "id": 1,
            "agent_type": "claude",
            "label": "prod-1",
            "credentials": _CIPHERTEXT,
            "token_limit": 100000,
            "enabled": 1,
            "status": "ok",
            "error_msg": None,
            "expires_at": datetime(2026, 1, 1),
            "used_at": datetime(2025, 12, 31),
            "created_at": datetime(2025, 1, 1),
            "updated_at": datetime(2025, 1, 1),
        }

        token = CredentialRecord.from_row(row)

        assert token.id == 1
        assert token.scope == "claude"
        assert token.label == "prod-1"
        assert token.credentials == _CREDS
        assert token.token_limit == 100000
        assert token.enabled is True
        assert token.status == CredentialStatus.OK
        assert token.error_msg is None
        assert token.expires_at == datetime(2026, 1, 1)
        assert token.used_at == datetime(2025, 12, 31)

    def test_from_row_errored(self):
        row = {
            "id": 2,
            "agent_type": "codex",
            "label": "codex-1",
            "credentials": None,
            "token_limit": 0,
            "enabled": 0,
            "status": "error",
            "error_msg": "OAuth token expired",
            "expires_at": None,
            "used_at": None,
            "created_at": datetime(2025, 1, 1),
            "updated_at": datetime(2025, 1, 1),
        }

        token = CredentialRecord.from_row(row)

        assert token.scope == "codex"
        assert token.enabled is False
        assert token.token_limit == 0
        assert token.status == CredentialStatus.ERROR
        assert token.error_msg == "OAuth token expired"
        assert token.expires_at is None
        assert token.used_at is None
        assert token.credentials is None

    def test_from_row_defaults_missing_health_fields(self):
        row = {
            "id": 3,
            "agent_type": "claude",
            "label": "legacy",
            "credentials": _CIPHERTEXT,
            "token_limit": 50000,
            "enabled": 1,
            "created_at": datetime(2025, 1, 1),
            "updated_at": datetime(2025, 1, 1),
        }

        token = CredentialRecord.from_row(row)

        assert token.status == CredentialStatus.OK
        assert token.error_msg is None
        assert token.expires_at is None
        assert token.used_at is None


class TestUsageSummary:
    def test_creation(self):
        summary = UsageSummary(credential_id=1, total_tokens=50000, call_count=10)
        assert summary.credential_id == 1
        assert summary.total_tokens == 50000
        assert summary.call_count == 10


class TestCredentialTypeAndPriority:
    def test_from_row_populates_type_and_priority(self):
        row = {
            "id": 1, "agent_type": "codex", "label": "x",
            "type": "codex_access_token", "priority": 5,
            "credentials": None,  # _decrypt_credentials handles None
            "token_limit": 0, "enabled": True,
            "status": "ok", "error_msg": None,
            "expires_at": None, "used_at": None,
            "created_at": datetime(2026, 1, 1),
            "updated_at": datetime(2026, 1, 1),
        }
        t = CredentialRecord.from_row(row)
        assert t.type == "codex_access_token"
        assert t.priority == 5

    def test_from_row_defaults_type_to_oauth_when_missing(self):
        """Defensive: if a row pre-dates migration 022, treat as oauth/priority=0."""
        row = {
            "id": 1, "agent_type": "codex", "label": "x",
            "credentials": None, "token_limit": 0, "enabled": True,
            "status": "ok", "error_msg": None, "expires_at": None, "used_at": None,
            "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1),
        }
        t = CredentialRecord.from_row(row)
        assert t.type == "oauth"
        assert t.priority == 0

    def test_from_row_defaults_when_columns_are_null(self):
        """Defensive: a NULL value in the type/priority column (e.g. rogue
        direct-SQL edit) must still resolve to the same safe defaults — this is
        the case `row.get(...) or default` handles that `row.get(..., default)`
        would not.
        """
        row = {
            "id": 1, "agent_type": "codex", "label": "x",
            "type": None, "priority": None,
            "credentials": None, "token_limit": 0, "enabled": True,
            "status": "ok", "error_msg": None, "expires_at": None, "used_at": None,
            "created_at": datetime(2026, 1, 1), "updated_at": datetime(2026, 1, 1),
        }
        t = CredentialRecord.from_row(row)
        assert t.type == "oauth"
        assert t.priority == 0
