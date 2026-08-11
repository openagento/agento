from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agento.framework.agent_manager.config import AgentManagerConfig
from agento.framework.agent_manager.models import (
    CredentialRecord,
    CredentialStatus,
    UsageSummary,
)
from agento.framework.harness import clear as _clear_harnesses
from tests.harness_fixtures import register_builtin_harnesses


@pytest.fixture(autouse=True)
def _register_harnesses():
    """Populate the harness registry so credential-env lookups resolve in unit
    tests that don't run the full ``bootstrap()`` module loader.
    """
    register_builtin_harnesses()
    yield
    _clear_harnesses()


@pytest.fixture
def agent_config(tmp_path):
    """AgentManagerConfig for tests that still need one."""
    return AgentManagerConfig()


def make_token(
    *,
    id: int = 1,
    agent_type: str = "claude",
    type: str = "oauth",
    label: str = "test-token",
    credentials: dict | None = None,
    token_limit: int = 100_000,
    enabled: bool = True,
    status: CredentialStatus = CredentialStatus.OK,
    priority: int = 0,
    error_msg: str | None = None,
    expires_at: datetime | None = None,
    used_at: datetime | None = None,
) -> CredentialRecord:
    """Helper to create CredentialRecord instances for testing."""
    now = datetime.now(UTC)
    if credentials is None:
        credentials = {"subscription_key": "sk-test"}
    return CredentialRecord(
        id=id,
        scope=agent_type,
        type=type,
        label=label,
        credentials=credentials,
        token_limit=token_limit,
        enabled=enabled,
        status=status,
        priority=priority,
        error_msg=error_msg,
        expires_at=expires_at,
        used_at=used_at,
        created_at=now,
        updated_at=now,
    )


def make_usage(
    token_id: int,
    total_tokens: int = 0,
    call_count: int = 0,
) -> UsageSummary:
    """Helper to create UsageSummary instances for testing."""
    return UsageSummary(
        credential_id=token_id,
        total_tokens=total_tokens,
        call_count=call_count,
    )
