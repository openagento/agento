from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CredentialStatus(Enum):
    OK = "ok"
    ERROR = "error"


@dataclass
class CredentialRecord:
    """One row of the ``credential`` table.

    ``scope`` is the open credential scope a harness declares in its ``di.json``
    (e.g. ``"claude"``) — it replaced the closed ``claude|codex`` enum. ``credentials``
    is the decrypted payload and is ``repr=False``: hiding it only inside the run
    context would not protect it when this object is logged directly.
    """

    id: int
    scope: str
    type: str
    label: str
    credentials: dict | None = field(repr=False, default=None)
    token_limit: int = 0
    enabled: bool = True
    status: CredentialStatus = CredentialStatus.OK
    priority: int = 0
    error_msg: str | None = None
    expires_at: datetime | None = None
    used_at: datetime | None = None
    created_at: datetime = datetime(2000, 1, 1)
    updated_at: datetime = datetime(2000, 1, 1)
    # Temporary usage-limit cooldown (credential.throttled_until). None when not throttled.
    throttled_until: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> CredentialRecord:
        return cls(
            id=row["id"],
            # `scope` is the column going forward; `agent_type` is the dual-written
            # legacy column, read only as a fallback for a pre-migration row.
            scope=row.get("scope") or row["agent_type"],
            type=row.get("type") or "oauth",
            label=row["label"],
            credentials=_decrypt_credentials(row.get("credentials")),
            token_limit=row["token_limit"],
            enabled=bool(row["enabled"]),
            status=CredentialStatus(row.get("status", "ok") or "ok"),
            priority=int(row.get("priority") or 0),
            error_msg=row.get("error_msg"),
            expires_at=row.get("expires_at"),
            throttled_until=row.get("throttled_until"),
            used_at=row.get("used_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _decrypt_credentials(raw: str | None) -> dict | None:
    if not raw:
        return None
    from ..encryptor import get_encryptor
    plaintext = get_encryptor().decrypt(raw)
    return json.loads(plaintext)


def encrypt_credentials(credentials: dict) -> str:
    """Encrypt a plaintext credentials dict for storage in credential.credentials."""
    from ..encryptor import get_encryptor
    return get_encryptor().encrypt(json.dumps(credentials))


@dataclass
class CredentiallessUsage:
    """Usage of runs made by a provider that requires no credential.

    Attributed by ``(harness, provider)`` since there is no credential row to hang it
    off. Reported separately from :class:`UsageSummary` so it can never be counted
    against a credential's limit.
    """

    harness: str | None
    provider: str | None
    total_tokens: int
    call_count: int


@dataclass
class UsageSummary:
    """Usage aggregated for one credential, or for credential-less runs.

    ``credential_id`` is ``None`` for the bucket of runs made by a provider that
    requires no credential — those rows carry ``(harness, provider)`` instead.
    """

    credential_id: int | None
    total_tokens: int
    call_count: int
