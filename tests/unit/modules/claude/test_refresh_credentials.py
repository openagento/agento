"""ClaudeWorkspaceAdapter.refresh_credentials — out-of-band refresh in a throw-away HOME.

Driven through the fake ``claude`` in tests/fixtures/fake_cli, which refreshes only when
``expiresAt`` is in the past, like the real CLI (A1 in DECISIONS.md 2026-10-08 AG-65).
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.framework.runner import client as runner_client
from agento.modules.claude.src import config as claude_config
from agento.modules.claude.src.config import ClaudeWorkspaceAdapter

_FAKE_CLI = Path(__file__).resolve().parents[3] / "fixtures" / "fake_cli"
_EPOCH = datetime(2000, 1, 1)

# The CLI starts in a runner (in-process here), never in the consumer.
pytestmark = pytest.mark.usefixtures("runner_server")


def _credential(type_: str = "oauth") -> CredentialRecord:
    near_ms = int(time.time() * 1000) + 10 * 60_000
    creds = {
        "subscription_key": "acc-OLD",
        "refresh_token": "rt-OLD",
        "raw_auth": {"credentials": {"claudeAiOauth": {
            "accessToken": "acc-OLD", "refreshToken": "rt-OLD", "expiresAt": near_ms,
        }}},
    }
    if type_ != "oauth":
        creds = {"api_key": "sk-test"}
    return CredentialRecord(
        id=7, scope="claude", type=type_, label="t", credentials=creds, token_limit=0,
        enabled=True, status=CredentialStatus.OK, priority=0, error_msg=None,
        expires_at=None, used_at=None, created_at=_EPOCH, updated_at=_EPOCH,
    )


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Run refresh_credentials with the fake CLI in ``mode``; returns (result, persist mock)."""
    monkeypatch.setenv("PATH", f"{_FAKE_CLI}{os.pathsep}{os.environ['PATH']}")
    tmp_root = runner_client.shared_tmp()
    persist = MagicMock()
    monkeypatch.setattr(claude_config, "update_refreshed_credentials", persist)

    def _run(mode: str, credential: CredentialRecord | None = None):
        monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
        try:
            return ClaudeWorkspaceAdapter().refresh_credentials(
                credential or _credential(), MagicMock()
            ), persist
        finally:
            # The decrypted credential never outlives the call, on any exit path.
            assert list(tmp_root.iterdir()) == []

    return _run


def _persisted_oauth(persist: MagicMock) -> dict:
    (_conn, cred_id, new_creds), _kw = persist.call_args
    assert cred_id == 7
    assert new_creds["expires_at"] is None
    return new_creds["raw_auth"]["credentials"]["claudeAiOauth"]


def test_rotation_is_persisted(run):
    refreshed, persist = run("rotate")
    assert refreshed is True
    oauth = _persisted_oauth(persist)
    assert (oauth["accessToken"], oauth["refreshToken"]) == ("acc-NEW", "rt-NEW")
    assert persist.call_args.args[2]["refresh_token"] == "rt-NEW"


@pytest.mark.parametrize("mode", ["access_only", "expiry_only"])
def test_a_partial_rotation_is_persisted(run, mode):
    refreshed, persist = run(mode)
    assert refreshed is True
    persist.assert_called_once()


def test_cli_that_did_not_rotate_returns_false_and_persists_nothing(run):
    refreshed, persist = run("noop")
    assert refreshed is False
    persist.assert_not_called()  # the past marker this adapter wrote is never stored


@pytest.mark.parametrize("mode", ["rotate_then_fail", "rotate_then_hang"])
def test_rotation_is_persisted_even_when_the_call_then_fails(run, monkeypatch, mode):
    monkeypatch.setattr(claude_config, "_REFRESH_TIMEOUT_SECONDS", 1)  # room for the fake CLI to rotate
    refreshed, persist = run(mode)
    assert refreshed is True
    assert _persisted_oauth(persist)["refreshToken"] == "rt-NEW"


def test_a_file_without_usable_tokens_is_not_persisted_and_the_failure_is_raised(run):
    """The refresh gate needs both tokens: a wiped file must not overwrite the stored
    login, and the non-zero exit is still classified (impl review round 1)."""
    with pytest.raises(RuntimeError, match=r"boom|rc=1"):
        run("wipe_then_fail")
    assert not claude_config.update_refreshed_credentials.called  # the fixture's mock


def test_permanent_auth_error_is_classified(run):
    with pytest.raises(AuthenticationError):
        run("auth_error")


def test_hang_without_rotation_raises_runtime_error(run, monkeypatch):
    monkeypatch.setattr(claude_config, "_REFRESH_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(RuntimeError, match="TimeoutExpired"):
        run("hang")


def test_missing_binary_raises_runtime_error(run, monkeypatch):
    monkeypatch.setattr(claude_config, "_REFRESH_ARGV", ["agento-no-such-cli"])
    with pytest.raises(RuntimeError, match="FileNotFoundError"):
        run("rotate")


def test_api_key_credential_is_not_refreshed(run, monkeypatch, tmp_path):
    dump = tmp_path / "env"
    monkeypatch.setenv("FAKE_CLI_ENV_DUMP", str(dump))
    refreshed, _ = run("rotate", _credential("anthropic_api_key"))
    assert refreshed is False
    assert not dump.exists()  # nothing was spawned


def test_spawned_cli_gets_its_own_home(run, monkeypatch, tmp_path):
    """The store never reaches the CLI because the runner holds none
    (``runner/test_runner_boundary.py``); this pins the HOME it is given."""
    dump = tmp_path / "env"
    monkeypatch.setenv("FAKE_CLI_ENV_DUMP", str(dump))
    monkeypatch.setenv("AGENTO_SSH_PRIVATE_KEY", "x")
    run("rotate")
    names = dump.read_text().split("\n")
    assert "AGENTO_SSH_PRIVATE_KEY" not in names
    assert "HOME" in names
