"""Tests for ReplaceErroredTokenCredentialsObserver — replaces stale credentials in the
builds that use the errored credential's scope with the next LRU healthy credential.

Only builds whose own agent_view resolves to that scope are touched, so a Codex failure
never rewrites a Claude build (``_builds_for_scope`` is stubbed here; its own matching
logic is covered in ``test_refresh_credentials_observer.py``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.modules.workspace_build.src.observers import (
    ReplaceErroredTokenCredentialsObserver,
)


@pytest.fixture
def build_root(tmp_path, monkeypatch):
    base = tmp_path / "workspace" / "build"
    base.mkdir(parents=True)
    monkeypatch.setattr(
        "agento.modules.workspace_build.src.observers.BUILD_DIR", str(base),
    )
    return base


def _make_build(base, ws: str, av: str, build_id: int = 1):
    build_dir = base / ws / av / "builds" / str(build_id)
    build_dir.mkdir(parents=True)
    current = base / ws / av / "current"
    current.symlink_to(build_dir)
    return build_dir


OBS = "agento.modules.workspace_build.src.observers"


def _stub_harness(monkeypatch, adapter, *, owner_of=None):
    """Point ``get_harness_for_scope`` at a RegisteredHarness-shaped stub."""
    registered = SimpleNamespace(adapter=SimpleNamespace(workspace_adapter=adapter))

    def _lookup(scope):
        if owner_of is not None and scope != owner_of:
            return None
        return registered

    monkeypatch.setattr(f"{OBS}.get_harness_for_scope", _lookup)


def _stub_builds(monkeypatch, builds):
    monkeypatch.setattr(f"{OBS}._builds_for_scope", lambda _scope: list(builds))


def _make_event(scope: str | None = "codex"):
    event = MagicMock()
    event.scope = scope
    event.credential_id = 6
    event.error_msg = "refresh token already used"
    event.job_id = None
    return event


def _make_token(token_id: int = 2, creds: dict | None = None) -> CredentialRecord:
    now = datetime.now(UTC).replace(tzinfo=None)
    return CredentialRecord(
        id=token_id,
        scope="codex",
        type="oauth",
        label="mklauza-codex",
        credentials=creds or {"subscription_key": "sk-healthy"},
        token_limit=0,
        enabled=True,
        status=CredentialStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=now,
        updated_at=now,
    )


def _patch_db(monkeypatch):
    """Wire DB connection + DatabaseConfig to MagicMocks so the observer can
    exit the ``conn.close()`` finally block without touching a real DB."""
    # The observer holds its own module-level reference, so patch it there.
    monkeypatch.setattr(f"{OBS}.get_connection", lambda _cfg: MagicMock())
    monkeypatch.setattr(
        "agento.framework.database_config.DatabaseConfig.from_env",
        classmethod(lambda cls: MagicMock()),
    )


class TestReplaceErroredTokenObserver:
    def test_writes_healthy_token_creds_into_every_current_build(
        self, build_root, monkeypatch,
    ):
        zyga = _make_build(build_root, "default", "zyga")
        mieszko = _make_build(build_root, "default", "mieszko")

        writer = MagicMock()
        _stub_harness(monkeypatch, writer)
        _stub_builds(monkeypatch, [zyga, mieszko])
        _patch_db(monkeypatch)

        healthy = _make_token(token_id=2, creds={"subscription_key": "sk-good"})
        resolver = MagicMock()
        resolver.resolve.return_value = healthy
        monkeypatch.setattr(
            "agento.framework.agent_manager.credential_resolver.CredentialResolver",
            lambda: resolver,
        )

        ReplaceErroredTokenCredentialsObserver().execute(_make_event())

        called_dirs = {call.args[0] for call in writer.write_credentials.call_args_list}
        assert called_dirs == {zyga, mieszko}
        for call in writer.write_credentials.call_args_list:
            assert call.args[1] is healthy
        # Resolver was asked for the event's own credential scope.
        assert resolver.resolve.call_args.args[1] == "codex"

    def test_noop_when_no_healthy_token(self, build_root, monkeypatch):
        build = _make_build(build_root, "default", "zyga")

        writer = MagicMock()
        _stub_harness(monkeypatch, writer)
        _stub_builds(monkeypatch, [build])
        _patch_db(monkeypatch)

        resolver = MagicMock()
        resolver.resolve.side_effect = RuntimeError(
            "All 1 enabled credentials for scope=codex are unhealthy",
        )
        monkeypatch.setattr(
            "agento.framework.agent_manager.credential_resolver.CredentialResolver",
            lambda: resolver,
        )

        ReplaceErroredTokenCredentialsObserver().execute(_make_event())

        writer.write_credentials.assert_not_called()

    def test_skips_when_event_has_no_scope(self, build_root, monkeypatch):
        build = _make_build(build_root, "default", "zyga")

        writer = MagicMock()
        _stub_harness(monkeypatch, writer)
        _stub_builds(monkeypatch, [build])

        ReplaceErroredTokenCredentialsObserver().execute(_make_event(scope=None))

        writer.write_credentials.assert_not_called()

    def test_skips_when_no_harness_owns_the_scope(self, build_root, monkeypatch):
        _make_build(build_root, "default", "zyga")

        writer = MagicMock()
        _stub_harness(monkeypatch, writer, owner_of="claude")

        # No harness owns scope 'codex' here — must not raise, must not write.
        ReplaceErroredTokenCredentialsObserver().execute(_make_event())

        writer.write_credentials.assert_not_called()

    def test_skips_unregistered_scope(self, build_root, monkeypatch):
        _make_build(build_root, "default", "zyga")

        writer = MagicMock()
        _stub_harness(monkeypatch, writer, owner_of="codex")

        ReplaceErroredTokenCredentialsObserver().execute(
            _make_event(scope="bogus_scope"),
        )

        writer.write_credentials.assert_not_called()

    def test_keeps_iterating_when_one_build_fails(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")
        mieszko = _make_build(build_root, "default", "mieszko")

        writer = MagicMock()
        writer.write_credentials.side_effect = [OSError("boom"), None]
        _stub_harness(monkeypatch, writer)
        _stub_builds(monkeypatch, [zyga, mieszko])
        _patch_db(monkeypatch)

        resolver = MagicMock()
        resolver.resolve.return_value = _make_token()
        monkeypatch.setattr(
            "agento.framework.agent_manager.credential_resolver.CredentialResolver",
            lambda: resolver,
        )

        ReplaceErroredTokenCredentialsObserver().execute(_make_event())

        called = {call.args[0] for call in writer.write_credentials.call_args_list}
        assert called == {zyga, mieszko}

    def test_no_build_root_is_noop(self, tmp_path, monkeypatch):
        missing = tmp_path / "no" / "workspace" / "build"
        monkeypatch.setattr(
            "agento.modules.workspace_build.src.observers.BUILD_DIR", str(missing),
        )
        writer = MagicMock()
        _stub_harness(monkeypatch, writer)
        _patch_db(monkeypatch)

        resolver = MagicMock()
        resolver.resolve.return_value = _make_token()
        monkeypatch.setattr(
            "agento.framework.agent_manager.credential_resolver.CredentialResolver",
            lambda: resolver,
        )

        ReplaceErroredTokenCredentialsObserver().execute(_make_event())

        writer.write_credentials.assert_not_called()

    def test_skips_when_replacement_has_no_credentials(self, build_root, monkeypatch):
        build = _make_build(build_root, "default", "zyga")

        writer = MagicMock()
        _stub_harness(monkeypatch, writer)
        _stub_builds(monkeypatch, [build])
        _patch_db(monkeypatch)

        bad = _make_token(creds=None)
        bad.credentials = None
        resolver = MagicMock()
        resolver.resolve.return_value = bad
        monkeypatch.setattr(
            "agento.framework.agent_manager.credential_resolver.CredentialResolver",
            lambda: resolver,
        )

        ReplaceErroredTokenCredentialsObserver().execute(_make_event())

        writer.write_credentials.assert_not_called()
