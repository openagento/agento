"""Tests for state dir + SSH materialization + persistent-path symlinks + retention GC."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agento.framework.agent_manager.models import CredentialRecord
from agento.modules.workspace_build.src.builder import (
    ensure_state_dir,
    gc_old_builds,
    link_persistent_paths,
    materialize_agent_credentials,
    materialize_ssh_identity,
)


@pytest.fixture
def workspace_base(tmp_path, monkeypatch):
    base = tmp_path / "workspace" / "build"
    base.mkdir(parents=True)
    monkeypatch.setattr(
        "agento.modules.workspace_build.src.builder.BUILD_DIR", str(base),
    )
    return base


class TestEnsureStateDir:
    def test_creates_state_root_and_subpaths(self, workspace_base):
        result = ensure_state_dir("it", "dev_01", [".claude/projects", ".codex/sessions"])
        expected = workspace_base / "it" / "dev_01" / "state"
        assert result == expected
        assert expected.is_dir()
        assert (expected / ".claude" / "projects").is_dir()
        assert (expected / ".codex" / "sessions").is_dir()

    def test_idempotent_on_second_call(self, workspace_base):
        ensure_state_dir("it", "dev_01", [".claude/projects"])
        # Drop a marker so we can check it survives a re-run
        marker = workspace_base / "it" / "dev_01" / "state" / ".claude" / "projects" / "preserved.jsonl"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("session-data")

        ensure_state_dir("it", "dev_01", [".claude/projects"])

        assert marker.is_file()
        assert marker.read_text() == "session-data"


class TestMaterializeSshIdentity:
    def test_writes_private_key_with_600_perms(self, tmp_path):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        resolved = {
            "agent_view/identity/ssh_private_key": "-----BEGIN FAKE KEY-----",
        }

        materialize_ssh_identity(build_dir, resolved)

        key_path = build_dir / ".ssh" / "id_rsa"
        assert key_path.is_file()
        assert key_path.read_text() == "-----BEGIN FAKE KEY-----\n"
        assert (key_path.stat().st_mode & 0o777) == 0o600
        assert (build_dir / ".ssh").stat().st_mode & 0o777 == 0o700

    def test_writes_public_key_plain(self, tmp_path):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        resolved = {
            "agent_view/identity/ssh_public_key": "ssh-ed25519 AAAA host",
        }

        materialize_ssh_identity(build_dir, resolved)

        pub_path = build_dir / ".ssh" / "id_rsa.pub"
        assert pub_path.is_file()
        assert pub_path.read_text() == "ssh-ed25519 AAAA host"

    def test_writes_ssh_config_and_known_hosts(self, tmp_path):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        resolved = {
            "agent_view/identity/ssh_config": "Host git\n  IdentityFile ~/.ssh/id_rsa\n",
            "agent_view/identity/ssh_known_hosts": "github.com ssh-ed25519 AAAA\n",
        }

        materialize_ssh_identity(build_dir, resolved)

        config_path = build_dir / ".ssh" / "config"
        known = build_dir / ".ssh" / "known_hosts"
        assert "IdentityFile" in config_path.read_text()
        assert (config_path.stat().st_mode & 0o777) == 0o600
        assert "github.com" in known.read_text()

    def test_does_nothing_when_no_overrides(self, tmp_path):
        build_dir = tmp_path / "build"
        build_dir.mkdir()

        materialize_ssh_identity(build_dir, {})

        assert not (build_dir / ".ssh").exists()


class TestLinkPersistentPaths:
    def test_creates_relative_symlinks(self, tmp_path):
        build_dir = tmp_path / "build"
        state_dir = tmp_path / "state"
        build_dir.mkdir()
        state_dir.mkdir()
        (state_dir / ".claude" / "projects").mkdir(parents=True)

        link_persistent_paths(build_dir, state_dir, [".claude/projects"])

        link = build_dir / ".claude" / "projects"
        assert link.is_symlink()
        assert link.resolve() == (state_dir / ".claude" / "projects").resolve()

    def test_replaces_existing_file(self, tmp_path):
        build_dir = tmp_path / "build"
        state_dir = tmp_path / "state"
        build_dir.mkdir()
        state_dir.mkdir()
        (state_dir / ".claude" / "projects").mkdir(parents=True)
        # Pre-existing file where symlink should go
        existing_file = build_dir / ".claude" / "projects"
        existing_file.parent.mkdir(parents=True, exist_ok=True)
        existing_file.write_text("old content")

        link_persistent_paths(build_dir, state_dir, [".claude/projects"])

        assert existing_file.is_symlink()

    def test_replaces_existing_directory(self, tmp_path):
        build_dir = tmp_path / "build"
        state_dir = tmp_path / "state"
        build_dir.mkdir()
        state_dir.mkdir()
        (state_dir / ".codex" / "sessions").mkdir(parents=True)
        (build_dir / ".codex" / "sessions").mkdir(parents=True)
        (build_dir / ".codex" / "sessions" / "stale.file").write_text("x")

        link_persistent_paths(build_dir, state_dir, [".codex/sessions"])

        assert (build_dir / ".codex" / "sessions").is_symlink()


class TestGcOldBuilds:
    def test_keeps_latest_n(self, tmp_path):
        builds = tmp_path / "builds"
        builds.mkdir()
        for i in range(1, 13):
            (builds / str(i)).mkdir()

        removed = gc_old_builds(builds, current_build_id=12, max_builds=10)

        assert sorted(removed) == [1, 2]
        assert not (builds / "1").exists()
        assert not (builds / "2").exists()
        assert (builds / "3").exists()
        assert (builds / "12").exists()

    def test_keeps_current_even_if_older_than_n(self, tmp_path):
        builds = tmp_path / "builds"
        builds.mkdir()
        for i in [1, 2, 3, 4, 5]:
            (builds / str(i)).mkdir()

        # Current is 2, keep max 2 → should keep 5, 4, plus 2 (current)
        removed = gc_old_builds(builds, current_build_id=2, max_builds=2)

        assert 2 not in removed
        assert (builds / "2").exists()
        assert (builds / "5").exists()
        assert (builds / "4").exists()
        assert 3 in removed
        assert 1 in removed

    def test_no_op_when_below_threshold(self, tmp_path):
        builds = tmp_path / "builds"
        builds.mkdir()
        for i in [1, 2, 3]:
            (builds / str(i)).mkdir()

        removed = gc_old_builds(builds, current_build_id=3, max_builds=10)

        assert removed == []
        for i in [1, 2, 3]:
            assert (builds / str(i)).exists()

    def test_ignores_non_numeric_entries(self, tmp_path):
        builds = tmp_path / "builds"
        builds.mkdir()
        (builds / "current").symlink_to(tmp_path)
        (builds / "1").mkdir()
        (builds / "2").mkdir()

        gc_old_builds(builds, current_build_id=2, max_builds=1)

        # Symlink 'current' is ignored (non-numeric name)
        assert (builds / "current").exists() or (builds / "current").is_symlink()


def _token(agent_type, credentials):
    from agento.framework.agent_manager.models import CredentialStatus
    now = datetime.now(UTC)
    return CredentialRecord(
        id=1, scope=agent_type, type="oauth", label="t", credentials=credentials,
        token_limit=0, enabled=True,
        status=CredentialStatus.OK, priority=0, error_msg=None, expires_at=None, used_at=None,
        created_at=now, updated_at=now,
    )


@pytest.mark.usefixtures("builtin_harnesses")
class TestMaterializeAgentCredentials:
    """One build ⇒ exactly one credential scope (least privilege).

    Iterating every registered harness handed a Claude view the Codex credential
    merely because the ``codex`` module was enabled; the build now resolves only the
    scope of its own effective ``(harness, provider)``.
    """

    @staticmethod
    def _resolver(monkeypatch, resolver):
        monkeypatch.setattr(
            "agento.framework.agent_manager.credential_resolver.CredentialResolver",
            lambda *a, **kw: resolver,
        )

    @staticmethod
    def _adapters(monkeypatch, **adapters):
        for target in (
            "agento.framework.harness.workspace_adapter_for",
            "agento.framework.harness.registry.workspace_adapter_for",
        ):
            monkeypatch.setattr(target, adapters.__getitem__)

    def test_writes_only_the_views_own_scope(self, tmp_path, monkeypatch):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        claude_writer = MagicMock()
        codex_writer = MagicMock()
        self._adapters(monkeypatch, claude=claude_writer, codex=codex_writer)

        resolver = MagicMock()
        resolver.resolve.return_value = _token("claude", {"subscription_key": "claude-tok"})
        self._resolver(monkeypatch, resolver)

        materialize_agent_credentials(
            conn=MagicMock(), build_dir=build_dir, harness="claude", provider="anthropic",
        )

        assert resolver.resolve.call_args.args[1] == "claude"
        assert claude_writer.write_credentials.call_args.args[1].credentials == {
            "subscription_key": "claude-tok",
        }
        codex_writer.write_credentials.assert_not_called()

    def test_no_harness_or_provider_writes_nothing(self, tmp_path, monkeypatch):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        claude_writer = MagicMock()
        self._adapters(monkeypatch, claude=claude_writer)
        resolver = MagicMock()
        self._resolver(monkeypatch, resolver)

        materialize_agent_credentials(
            conn=MagicMock(), build_dir=build_dir, harness=None, provider=None,
        )

        resolver.resolve.assert_not_called()
        claude_writer.write_credentials.assert_not_called()

    def test_skips_provider_without_enabled_credential(self, tmp_path, monkeypatch):
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        claude_writer = MagicMock()
        self._adapters(monkeypatch, claude=claude_writer)

        resolver = MagicMock()
        resolver.resolve.side_effect = RuntimeError("no enabled credentials")
        self._resolver(monkeypatch, resolver)

        materialize_agent_credentials(
            conn=MagicMock(), build_dir=build_dir, harness="claude", provider="anthropic",
        )

        claude_writer.write_credentials.assert_not_called()

    def test_swallows_write_errors(self, tmp_path, monkeypatch, caplog):
        import logging

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        claude_writer = MagicMock()
        claude_writer.write_credentials.side_effect = RuntimeError("boom")
        self._adapters(monkeypatch, claude=claude_writer)

        resolver = MagicMock()
        resolver.resolve.return_value = _token("claude", {"subscription_key": "x"})
        self._resolver(monkeypatch, resolver)

        with caplog.at_level(logging.WARNING, logger="agento.modules.workspace_build.src.builder"):
            materialize_agent_credentials(
                conn=MagicMock(), build_dir=build_dir, harness="claude", provider="anthropic",
            )

        assert "failed to write credentials" in caplog.text

    def _materialize_codex(self, monkeypatch, build_dir, expiry):
        codex_writer = MagicMock()
        self._adapters(monkeypatch, codex=codex_writer)
        resolver = MagicMock()
        resolver.resolve.return_value = _token("codex", {
            "raw_auth": {"tokens": {"refresh_token": "r", "expiry": expiry}},
        })
        self._resolver(monkeypatch, resolver)
        materialize_agent_credentials(
            conn=MagicMock(), build_dir=build_dir, harness="codex", provider="openai",
        )
        return codex_writer

    def test_logs_warning_when_credential_is_expired(self, tmp_path, monkeypatch, caplog):
        import logging
        from datetime import timedelta

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        with caplog.at_level(logging.WARNING, logger="agento.modules.workspace_build.src.builder"):
            codex_writer = self._materialize_codex(monkeypatch, build_dir, past)

        assert "expired" in caplog.text.lower()
        codex_writer.write_credentials.assert_called_once()  # Still writes despite warning

    def test_no_warning_when_credential_is_not_expired(self, tmp_path, monkeypatch, caplog):
        import logging
        from datetime import timedelta

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

        with caplog.at_level(logging.WARNING, logger="agento.modules.workspace_build.src.builder"):
            self._materialize_codex(monkeypatch, build_dir, future)

        assert "expired" not in caplog.text.lower()

    @pytest.mark.parametrize("bad_expiry", ["not-a-date", 12345, "2026-01-01T00:00:00"])
    def test_silently_ignores_unparseable_expiry(self, bad_expiry, tmp_path, monkeypatch, caplog):
        import logging

        build_dir = tmp_path / "build"
        build_dir.mkdir()

        with caplog.at_level(logging.WARNING, logger="agento.modules.workspace_build.src.builder"):
            codex_writer = self._materialize_codex(monkeypatch, build_dir, bad_expiry)

        # Unparseable/naive expiry must: not warn, not raise, still call write_credentials
        assert "expired" not in caplog.text.lower()
        codex_writer.write_credentials.assert_called_once()
