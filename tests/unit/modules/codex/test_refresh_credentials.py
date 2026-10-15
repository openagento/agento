"""CodexWorkspaceAdapter.refresh_credentials — out-of-band refresh in a throw-away HOME.

Driven through the fake ``codex`` in tests/fixtures/fake_cli, which refreshes only when
``last_refresh`` is old, like the real CLI (A2 in DECISIONS.md 2026-10-08 AG-65).
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.errors import AuthenticationError, TransientAuthError
from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.modules.codex.src import config as codex_config
from agento.modules.codex.src.config import CodexWorkspaceAdapter

_FAKE_CLI = Path(__file__).resolve().parents[3] / "fixtures" / "fake_cli"
_EPOCH = datetime(2000, 1, 1)


def _jwt(exp: float) -> str:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64({'exp': int(exp), 'sub': 'u'})}.sig"


_ACC_OLD = _jwt(time.time() + 3600)  # still valid: codex 0.157 would not refresh it alone


def _credential(type_: str = "oauth", access: str = _ACC_OLD) -> CredentialRecord:
    creds = {
        "subscription_key": access,
        "refresh_token": "rt-OLD",
        "raw_auth": {
            "tokens": {"access_token": access, "refresh_token": "rt-OLD"},
            "last_refresh": "2026-10-08T00:00:00Z",
        },
    }
    if type_ != "oauth":
        creds = {"api_key": "sk-test"}
    return CredentialRecord(
        id=7, scope="codex", type=type_, label="t", credentials=creds, token_limit=0,
        enabled=True, status=CredentialStatus.OK, priority=0, error_msg=None,
        expires_at=None, used_at=None, created_at=_EPOCH, updated_at=_EPOCH,
    )


@pytest.fixture
def run(monkeypatch, tmp_path):
    """Run refresh_credentials with the fake CLI in ``mode``; returns (result, persist mock)."""
    monkeypatch.setenv("PATH", f"{_FAKE_CLI}{os.pathsep}{os.environ['PATH']}")
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_root))
    persist = MagicMock()
    monkeypatch.setattr(codex_config, "update_refreshed_credentials", persist)

    def _run(mode: str, credential: CredentialRecord | None = None):
        monkeypatch.setenv("FAKE_CODEX_MODE", mode)
        try:
            return CodexWorkspaceAdapter().refresh_credentials(
                credential or _credential(), MagicMock()
            ), persist
        finally:
            # The decrypted credential never outlives the call, on any exit path.
            assert list(tmp_root.iterdir()) == []

    return _run


def _persisted_tokens(persist: MagicMock) -> dict:
    (_conn, cred_id, new_creds), _kw = persist.call_args
    assert cred_id == 7
    return new_creds["raw_auth"]["tokens"]


def test_rotation_is_persisted(run):
    refreshed, persist = run("rotate")
    assert refreshed is True
    assert _persisted_tokens(persist) == {"access_token": "acc-NEW", "refresh_token": "rt-NEW"}
    assert persist.call_args.args[2]["refresh_token"] == "rt-NEW"


@pytest.mark.parametrize(
    ("mode", "access"),
    [("access_only", _ACC_OLD), ("last_refresh_only", "acc-OLD")],  # the marker is the opaque path's
)
def test_a_partial_rotation_is_persisted(run, mode, access):
    refreshed, persist = run(mode, _credential(access=access))
    assert refreshed is True
    persist.assert_called_once()


def test_cli_that_did_not_rotate_returns_false_and_persists_nothing(run):
    refreshed, persist = run("noop")
    assert refreshed is False
    persist.assert_not_called()  # the stale marker this adapter wrote is never stored


@pytest.mark.parametrize("mode", ["rotate_then_fail", "rotate_then_hang"])
def test_rotation_is_persisted_even_when_the_call_then_fails(run, monkeypatch, mode):
    monkeypatch.setattr(codex_config, "_REFRESH_TIMEOUT_SECONDS", 1)  # room for the fake CLI to rotate
    refreshed, persist = run(mode)
    assert refreshed is True
    assert _persisted_tokens(persist)["refresh_token"] == "rt-NEW"


def test_a_file_without_usable_tokens_is_not_persisted_and_the_failure_is_raised(run):
    """The refresh gate needs both tokens: a wiped file must not overwrite the stored
    login, and the non-zero exit is still classified (impl review round 1)."""
    with pytest.raises(RuntimeError, match=r"boom|rc=1"):
        run("wipe_then_fail")
    assert not codex_config.update_refreshed_credentials.called  # the fixture's mock


@pytest.mark.parametrize("mode", ["null_tokens_auth_error", "list_tokens_auth_error"])
def test_a_tokens_value_that_is_not_an_object_still_classifies_the_failure(run, mode):
    """A malformed ``tokens`` must not raise a TypeError past the classification (impl
    review round 2): the auth failure reaches the poison path, nothing is persisted."""
    with pytest.raises(AuthenticationError):
        run(mode)
    assert not codex_config.update_refreshed_credentials.called  # the fixture's mock


def test_a_valid_jwt_is_refreshed_by_forcing_its_exp_into_the_past(run):
    """codex 0.157 refreshes on a past JWT ``exp``, not on an old ``last_refresh``."""
    refreshed, persist = run("rotate")
    assert refreshed is True
    assert _persisted_tokens(persist)["access_token"] == "acc-NEW"


def test_an_opaque_access_token_still_uses_the_last_refresh_marker(run):
    refreshed, persist = run("rotate", _credential(access="acc-OLD"))
    assert refreshed is True
    assert _persisted_tokens(persist)["refresh_token"] == "rt-NEW"


def test_the_edited_access_token_is_never_persisted(run):
    """The CLI wrote last_refresh but did not refresh: the file still holds the token with
    the past ``exp``, which must not reach the store."""
    refreshed, persist = run("last_refresh_only")
    assert refreshed is False
    persist.assert_not_called()


def test_a_refused_refresh_token_on_stderr_is_a_transient_auth_error(run):
    with pytest.raises(TransientAuthError, match="refresh token was refused"):
        run("dead_refresh")
    assert not codex_config.update_refreshed_credentials.called  # the fixture's mock


def test_permanent_auth_error_is_classified(run):
    with pytest.raises(AuthenticationError):
        run("auth_error")


def test_hang_without_rotation_raises_runtime_error(run, monkeypatch):
    monkeypatch.setattr(codex_config, "_REFRESH_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(RuntimeError, match="TimeoutExpired"):
        run("hang")


def test_api_key_credential_is_not_refreshed(run, monkeypatch, tmp_path):
    dump = tmp_path / "env"
    monkeypatch.setenv("FAKE_CLI_ENV_DUMP", str(dump))
    refreshed, _ = run("rotate", _credential("openai_api_key"))
    assert refreshed is False
    assert not dump.exists()  # nothing was spawned


def test_spawned_cli_gets_no_credential_store_env(run, monkeypatch, tmp_path):
    dump = tmp_path / "env"
    monkeypatch.setenv("FAKE_CLI_ENV_DUMP", str(dump))
    monkeypatch.setenv("MYSQL_PASSWORD", "x")
    run("rotate")
    names = dump.read_text().split("\n")
    assert "MYSQL_PASSWORD" not in names
    assert "HOME" in names
