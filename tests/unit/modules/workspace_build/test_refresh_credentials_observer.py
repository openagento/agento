"""Tests for RefreshBuildCredentialsObserver — keeps existing build dirs in sync with
the ``credential`` table after ``credential:refresh`` / ``credential:register``.

The observer resolves the WorkspaceAdapter off the harness that OWNS the event's
credential scope, and only touches builds whose agent_view actually resolves to that
scope (``_builds_for_scope``) — a Codex refresh must never rewrite a Claude build.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.models import CredentialRecord
from agento.modules.workspace_build.src.observers import (
    RefreshBuildCredentialsObserver,
    _credential_from_event,
)

OBS = "agento.modules.workspace_build.src.observers"


@pytest.fixture
def build_root(tmp_path, monkeypatch):
    base = tmp_path / "workspace" / "build"
    base.mkdir(parents=True)
    monkeypatch.setattr(f"{OBS}.BUILD_DIR", str(base))
    return base


def _make_build(base, ws: str, av: str, build_id: int = 1):
    """Create ``<ws>/<av>/builds/<n>`` and a ``current`` symlink pointing to it."""
    build_dir = base / ws / av / "builds" / str(build_id)
    build_dir.mkdir(parents=True)
    (base / ws / av / "current").symlink_to(build_dir)
    return build_dir


def _stub_harness(monkeypatch, adapter, *, owner_of=None):
    """Point ``get_harness_for_scope`` at a RegisteredHarness-shaped stub.

    ``owner_of`` restricts which scope the stub owns; any other scope resolves to
    ``None``, which is what makes the observer a no-op.
    """
    registered = SimpleNamespace(adapter=SimpleNamespace(workspace_adapter=adapter))

    def _lookup(scope):
        if owner_of is not None and scope != owner_of:
            return None
        return registered

    monkeypatch.setattr(f"{OBS}.get_harness_for_scope", _lookup)
    return registered


def _stub_builds(monkeypatch, builds):
    monkeypatch.setattr(f"{OBS}._builds_for_scope", lambda _scope: list(builds))


def _make_event(scope="claude", credentials=None, type_="oauth"):
    event = MagicMock()
    event.scope = scope
    event.credentials = credentials or {"subscription_key": "sk-new"}
    event.type = type_
    return event


class TestRefreshObserver:
    def test_writes_credentials_into_every_matching_build(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")
        mieszko = _make_build(build_root, "default", "mieszko")

        adapter = MagicMock()
        _stub_harness(monkeypatch, adapter)
        _stub_builds(monkeypatch, [zyga, mieszko])

        RefreshBuildCredentialsObserver().execute(
            _make_event(credentials={"subscription_key": "sk-new"})
        )

        called_dirs = {call.args[0] for call in adapter.write_credentials.call_args_list}
        assert called_dirs == {zyga, mieszko}
        for call in adapter.write_credentials.call_args_list:
            credential = call.args[1]
            assert isinstance(credential, CredentialRecord)
            assert credential.credentials == {"subscription_key": "sk-new"}

    def test_only_the_owning_harnesss_adapter_is_used(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")

        claude_adapter = MagicMock()
        codex_adapter = MagicMock()
        claude = SimpleNamespace(
            adapter=SimpleNamespace(workspace_adapter=claude_adapter)
        )
        codex = SimpleNamespace(adapter=SimpleNamespace(workspace_adapter=codex_adapter))
        monkeypatch.setattr(
            f"{OBS}.get_harness_for_scope",
            lambda scope: claude if scope == "claude" else codex,
        )
        _stub_builds(monkeypatch, [zyga])

        RefreshBuildCredentialsObserver().execute(_make_event(scope="claude"))

        assert claude_adapter.write_credentials.call_count == 1
        codex_adapter.write_credentials.assert_not_called()

    def test_skips_when_event_has_no_scope(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")
        adapter = MagicMock()
        _stub_harness(monkeypatch, adapter)
        _stub_builds(monkeypatch, [zyga])

        event = MagicMock()
        event.scope = None
        event.credentials = {"subscription_key": "sk-new"}

        RefreshBuildCredentialsObserver().execute(event)

        adapter.write_credentials.assert_not_called()

    def test_skips_when_no_harness_owns_the_scope(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")
        adapter = MagicMock()
        _stub_harness(monkeypatch, adapter, owner_of="claude")
        _stub_builds(monkeypatch, [zyga])

        # Must not raise, and must not write with someone else's adapter.
        RefreshBuildCredentialsObserver().execute(_make_event(scope="unowned"))

        adapter.write_credentials.assert_not_called()

    def test_no_matching_build_is_a_noop(self, build_root, monkeypatch):
        _make_build(build_root, "default", "zyga")
        adapter = MagicMock()
        _stub_harness(monkeypatch, adapter)
        _stub_builds(monkeypatch, [])

        RefreshBuildCredentialsObserver().execute(_make_event())

        adapter.write_credentials.assert_not_called()

    def test_keeps_iterating_when_one_build_fails(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")
        mieszko = _make_build(build_root, "default", "mieszko")

        adapter = MagicMock()
        adapter.write_credentials.side_effect = [OSError("boom"), None]
        _stub_harness(monkeypatch, adapter)
        _stub_builds(monkeypatch, [zyga, mieszko])

        RefreshBuildCredentialsObserver().execute(_make_event())

        called = {call.args[0] for call in adapter.write_credentials.call_args_list}
        assert called == {zyga, mieszko}


class TestBuildsForScope:
    """``_builds_for_scope`` is the least-privilege gate: a build is only touched when
    its own agent_view resolves to the event's credential scope."""

    def test_skips_dangling_current_symlink(self, build_root, monkeypatch):
        current = build_root / "default" / "ghost" / "current"
        current.parent.mkdir(parents=True)
        current.symlink_to(build_root / "default" / "ghost" / "builds" / "999")

        from agento.modules.workspace_build.src.observers import _builds_for_scope

        # No candidate survives resolution, so the DB is never even opened.
        monkeypatch.setattr(
            f"{OBS}.get_connection",
            lambda *_a, **_kw: pytest.fail("DB must not be opened with no candidates"),
        )
        assert _builds_for_scope("claude") == []

    def test_missing_build_root_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(f"{OBS}.BUILD_DIR", str(tmp_path / "no" / "build"))

        from agento.modules.workspace_build.src.observers import _builds_for_scope

        assert _builds_for_scope("claude") == []

    def test_only_views_resolving_to_the_scope_match(self, build_root, monkeypatch):
        claude_build = _make_build(build_root, "default", "zyga")
        _make_build(build_root, "default", "mieszko")

        monkeypatch.setattr(f"{OBS}.get_connection", lambda *_a, **_kw: MagicMock())
        monkeypatch.setattr(
            "agento.framework.workspace.get_agent_view_by_code",
            lambda _conn, code: SimpleNamespace(id={"zyga": 1, "mieszko": 2}[code]),
        )
        monkeypatch.setattr(
            "agento.framework.agent_view_runtime.resolve_agent_view_runtime",
            lambda _conn, av_id: SimpleNamespace(
                harness="claude" if av_id == 1 else "codex",
                provider="anthropic" if av_id == 1 else "openai",
            ),
        )
        monkeypatch.setattr(
            "agento.framework.harness.resolve_credential_scope",
            lambda harness, _provider: harness,
        )

        from agento.modules.workspace_build.src.observers import _builds_for_scope

        assert _builds_for_scope("claude") == [claude_build]


class TestCredentialFromEvent:
    def test_type_propagated_from_event(self):
        credential = _credential_from_event(
            _make_event(scope="claude", type_="codex_access_token")
        )
        assert credential.type == "codex_access_token"
        assert credential.scope == "claude"

    def test_type_defaults_to_oauth_when_absent(self):
        event = MagicMock(spec=["scope", "credential_id", "label", "credentials"])
        event.scope = "claude"
        event.credential_id = 1
        event.label = "test"
        event.credentials = {}

        credential = _credential_from_event(event)

        assert credential.type == "oauth"
        assert credential.id == 1

    def test_api_key_credential_is_threaded_into_builds(self, build_root, monkeypatch):
        zyga = _make_build(build_root, "default", "zyga")
        adapter = MagicMock()
        _stub_harness(monkeypatch, adapter)
        _stub_builds(monkeypatch, [zyga])

        RefreshBuildCredentialsObserver().execute(
            _make_event(
                scope="codex",
                credentials={"api_key": "sk-openai"},
                type_="openai_api_key",
            )
        )

        assert adapter.write_credentials.call_count == 1
        credential = adapter.write_credentials.call_args.args[1]
        assert credential.type == "openai_api_key"
        assert credential.credentials == {"api_key": "sk-openai"}
