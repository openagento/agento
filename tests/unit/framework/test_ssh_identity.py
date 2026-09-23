"""Per-run SSH identity helpers — the private key is never a file."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.ssh_identity import (
    MAX_SSH_PRIVATE_KEY_BYTES,
    RUN_OWNED_SSH_ENV_VARS,
    SSH_CONFIG_PATH,
    SSH_KNOWN_HOSTS_PATH,
    SSH_PRIVATE_KEY_PATH,
    SSH_PUBLIC_KEY_PATH,
    SSH_TTL_LOCAL_RUN,
    ResolvedSshIdentity,
    SshKeyPurgeError,
    find_private_keys,
    git_ssh_command_env,
    materialize_ssh_public_identity,
    prune_stale_build_keys,
    resolve_ssh_identity,
    scrub_ssh_private_key,
    ssh_private_key_env,
    ssh_run_env,
    without_run_owned_ssh_env,
)

_REPO = Path(__file__).resolve().parents[3]


def _keys(root) -> list[Path]:
    """The scan's findings, asserting it could read the whole tree."""
    scan = find_private_keys(root)
    assert scan.unreadable == (), scan.unreadable
    return list(scan.keys)

_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQy\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


class _Svc:
    """Counting config-service stub — `.get()` calls are the assertion in some tests."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = values or {}
        self.calls: list[str] = []

    def get(self, path):
        self.calls.append(path)
        return self.values.get(path)


class TestMaterializePublicIdentity:
    def test_writes_only_the_non_secret_files(self, tmp_path):
        resolved = ResolvedSshIdentity(
            private_key=_PEM,
            public_key="ssh-ed25519 AAAA host",
            config="Host github.com\n",
            known_hosts="github.com ssh-ed25519 AAAA\n",
        )
        materialize_ssh_public_identity(tmp_path, resolved)

        ssh = tmp_path / ".ssh"
        assert ssh.stat().st_mode & 0o777 == 0o700
        assert (ssh / "id_rsa.pub").read_text() == "ssh-ed25519 AAAA host"
        assert (ssh / "config").read_text() == "Host github.com\n"
        assert (ssh / "known_hosts").read_text() == "github.com ssh-ed25519 AAAA\n"
        for name in ("id_rsa.pub", "config", "known_hosts"):
            assert (ssh / name).stat().st_mode & 0o777 == 0o600
        # The whole point: no private key file, even though one is configured.
        assert not (ssh / "id_rsa").exists()

    def test_no_id_rsa_is_written_for_any_input(self, tmp_path):
        materialize_ssh_public_identity(tmp_path, ResolvedSshIdentity(private_key=_PEM))
        assert not (tmp_path / ".ssh").exists()

    def test_noop_when_all_non_secret_values_are_empty(self, tmp_path):
        materialize_ssh_public_identity(tmp_path, ResolvedSshIdentity())
        assert not (tmp_path / ".ssh").exists()


class TestPrivateKeyEnv:
    def test_appends_a_trailing_newline(self):
        env = ssh_private_key_env(ResolvedSshIdentity(private_key="KEY"))
        assert env == {"AGENTO_SSH_PRIVATE_KEY": "KEY\n"}

    def test_does_not_double_the_newline(self):
        env = ssh_private_key_env(ResolvedSshIdentity(private_key="KEY\n"))
        assert env == {"AGENTO_SSH_PRIVATE_KEY": "KEY\n"}

    def test_empty_without_a_key(self):
        assert ssh_private_key_env(ResolvedSshIdentity()) == {}


class TestScrubPrivateKey:
    def test_removes_a_legacy_key_and_keeps_the_rest(self, tmp_path):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text(_PEM)
        (ssh / "id_rsa.pub").write_text("pub")
        (ssh / "config").write_text("cfg")
        (ssh / "known_hosts").write_text("kh")

        scrub_ssh_private_key(tmp_path)

        assert not (ssh / "id_rsa").exists()
        assert (ssh / "id_rsa.pub").is_file()
        assert (ssh / "config").is_file()
        assert (ssh / "known_hosts").is_file()

    def test_noop_when_absent(self, tmp_path):
        scrub_ssh_private_key(tmp_path)  # no .ssh at all
        (tmp_path / ".ssh").mkdir()
        scrub_ssh_private_key(tmp_path)  # dir but no key

    def test_raises_on_an_unlink_error_rather_than_swallowing_it(self, tmp_path, caplog):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text(_PEM)
        with patch.object(Path, "unlink", side_effect=PermissionError("denied")), \
             caplog.at_level(logging.ERROR), \
             pytest.raises(PermissionError):
            scrub_ssh_private_key(tmp_path)
        assert "SECURITY" in caplog.text

    def test_raises_when_the_path_survives_the_unlink(self, tmp_path, caplog):
        """Fail closed: a key we could not remove is a key the agent can read."""
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "id_rsa").write_text(_PEM)
        with patch.object(Path, "unlink", return_value=None), \
             caplog.at_level(logging.ERROR), \
             pytest.raises(RuntimeError):
            scrub_ssh_private_key(tmp_path)
        assert "still exists" in caplog.text


class TestPruneStaleBuildKeys:
    def test_strips_every_generation_and_returns_what_it_removed(self, tmp_path):
        for gen in (4, 10, 16):
            ssh = tmp_path / str(gen) / ".ssh"
            ssh.mkdir(parents=True)
            (ssh / "id_rsa").write_text(_PEM)
            (ssh / "id_rsa.pub").write_text("pub")
        # A renamed copy is caught by content, not by the name .ssh/id_rsa.
        (tmp_path / "16" / "stolen.pem").write_text(_PEM)

        removed = prune_stale_build_keys(tmp_path)

        assert len(removed) == 4
        assert _keys(tmp_path) == []
        assert (tmp_path / "4" / ".ssh" / "id_rsa.pub").is_file()

    def test_noop_on_a_clean_tree(self, tmp_path):
        (tmp_path / "1").mkdir()
        assert prune_stale_build_keys(tmp_path) == []

    def test_noop_when_the_tree_does_not_exist(self, tmp_path):
        assert prune_stale_build_keys(tmp_path / "nope") == []


class TestFindPrivateKeys:
    def test_finds_a_renamed_copy_by_content(self, tmp_path):
        target = tmp_path / "work" / ".sshkey" / "id_rsa_backup"
        target.parent.mkdir(parents=True)
        target.write_text(_PEM)
        assert _keys(tmp_path) == [target]

    def test_finds_an_id_rsa_by_name_even_when_truncated(self, tmp_path):
        target = tmp_path / "id_rsa"
        target.write_text("-----BEGIN OPENSSH PRIVATE")  # the 36-byte PROD case
        assert _keys(tmp_path) == [target]

    def test_finds_a_pem_in_a_file_larger_than_64_kib(self, tmp_path):
        """An earlier revision skipped large files, so a padded key survived a purge."""
        target = tmp_path / "padded.pem"
        target.write_text(_PEM + "A" * 200_000)
        assert _keys(tmp_path) == [target]

    def test_ignores_an_unrelated_large_binary(self, tmp_path):
        (tmp_path / "blob.bin").write_bytes(bytes(range(256)) * 500)
        assert _keys(tmp_path) == []

    def test_does_not_follow_symlinks(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text(_PEM)
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "linked").symlink_to(outside / "id_rsa")
        (tree / "linked_dir").symlink_to(outside)
        assert _keys(tree) == []

    def test_ignores_an_incomplete_envelope(self, tmp_path):
        (tmp_path / "partial").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n")
        assert _keys(tmp_path) == []

    @pytest.mark.parametrize("fixture", [
        "tests/unit/modules/agent_view/test_identity_commands.py",
        "src/agento/toolbox/tests/outlook-graph-auth.test.js",
        "tests/unit/framework/test_config_path_slash_key.py",
    ])
    def test_ignores_a_source_file_that_merely_quotes_a_pem_header(self, tmp_path, fixture):
        """The regression that matters most: prune deletes WITHOUT confirmation, and an
        agent clones repositories (this one included) into workspace/artifacts."""
        source = _REPO / fixture
        copy = tmp_path / Path(fixture).name
        copy.write_bytes(source.read_bytes())
        assert _keys(tmp_path) == []


class TestScanWindowCoversEveryAcceptedKey:
    """The detection window must not be narrower than the key size the system accepts.

    Content detection exists for RENAMED copies — the exact name `id_rsa` is matched
    separately — so a key the system itself accepted, renamed and left on disk must be
    found whatever its length up to the configured maximum.
    """

    def _key_of(self, body_bytes: int) -> str:
        body = "\n".join("A" * 70 for _ in range(body_bytes // 71 + 1))
        return (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            + body
            + "\n-----END OPENSSH PRIVATE KEY-----\n"
        )

    @pytest.mark.parametrize("body_bytes", [9 * 1024, MAX_SSH_PRIVATE_KEY_BYTES - 512])
    def test_a_renamed_key_at_the_accepted_size_is_found(self, tmp_path, body_bytes):
        pem = self._key_of(body_bytes)
        assert len(pem.encode()) > 8192, "the case only bites past the old window"
        renamed = tmp_path / "backup.dat"
        renamed.write_text(pem)
        assert _keys(tmp_path) == [renamed]

    def test_the_window_is_derived_from_the_configured_maximum(self):
        from agento.framework.ssh_identity import _SCAN_PREFIX_BYTES

        # Not an independent literal: a raised maximum must widen the window with it.
        assert _SCAN_PREFIX_BYTES > MAX_SSH_PRIVATE_KEY_BYTES

    def test_a_file_far_larger_than_the_window_is_still_bounded_but_found(self, tmp_path):
        """Padding after the envelope must not defeat detection, nor read the whole file."""
        padded = tmp_path / "padded.bin"
        padded.write_text(self._key_of(1024) + "Z" * 2_000_000)
        assert _keys(tmp_path) == [padded]


class TestScanAndPruneFailClosed:
    """A scan that cannot read everything, or a key it cannot unlink, must not look clean.

    One class: "the code reports absence of a key it never managed to look at". Both
    entry points (scan, prune) and both causes (unreadable path, failed unlink).
    """

    @staticmethod
    def _sealed_dir(tmp_path: Path) -> Path:
        sealed = tmp_path / "sealed"
        sealed.mkdir()
        (sealed / "id_rsa").write_text(_PEM)
        sealed.chmod(0o000)
        return sealed

    def test_an_unlistable_directory_is_reported_not_skipped(self, tmp_path):
        sealed = self._sealed_dir(tmp_path)
        try:
            scan = find_private_keys(tmp_path)
        finally:
            sealed.chmod(0o700)
        assert scan.keys == ()
        assert scan.unreadable and "sealed" in scan.unreadable[0]

    def test_an_unreadable_file_is_reported_not_skipped(self, tmp_path):
        target = tmp_path / "secret.pem"
        target.write_text(_PEM)
        target.chmod(0o000)
        try:
            scan = find_private_keys(tmp_path)
        finally:
            target.chmod(0o600)
        assert scan.keys == ()
        assert scan.unreadable and "secret.pem" in scan.unreadable[0]

    def test_a_file_that_vanished_mid_walk_is_not_an_error(self, tmp_path):
        """An agent run writes under workspace/artifacts while a purge walks it."""
        (tmp_path / "gone").write_text("x")
        real_open = Path.open

        def _vanish(self, *a, **kw):
            if self.name == "gone":
                raise FileNotFoundError(2, "No such file or directory", str(self))
            return real_open(self, *a, **kw)

        with patch.object(Path, "open", _vanish):
            scan = find_private_keys(tmp_path)
        assert scan == type(scan)()

    def test_prune_raises_when_a_key_cannot_be_unlinked(self, tmp_path):
        gen = tmp_path / "15" / ".ssh"
        gen.mkdir(parents=True)
        (gen / "id_rsa").write_text(_PEM)
        gen.chmod(0o500)  # readable+listable, not writable: unlink fails
        try:
            with pytest.raises(SshKeyPurgeError) as exc:
                prune_stale_build_keys(tmp_path)
        finally:
            gen.chmod(0o700)
        assert "id_rsa" in str(exc.value)
        assert "workspace:ssh-purge" in str(exc.value)

    def test_prune_raises_when_the_scan_was_incomplete(self, tmp_path):
        sealed = self._sealed_dir(tmp_path)
        try:
            with pytest.raises(SshKeyPurgeError):
                prune_stale_build_keys(tmp_path)
        finally:
            sealed.chmod(0o700)

    def test_prune_removes_everything_it_can_before_raising(self, tmp_path):
        removable = tmp_path / "10" / ".ssh"
        removable.mkdir(parents=True)
        (removable / "id_rsa").write_text(_PEM)
        stuck = tmp_path / "15" / ".ssh"
        stuck.mkdir(parents=True)
        (stuck / "id_rsa").write_text(_PEM)
        stuck.chmod(0o500)
        try:
            with pytest.raises(SshKeyPurgeError):
                prune_stale_build_keys(tmp_path)
        finally:
            stuck.chmod(0o700)
        assert not (removable / "id_rsa").exists()


class TestRunOwnedSshEnv:
    """A run's SSH env may only come from that run's resolved identity.

    `ssh_run_env` returns `{}` for an identity-less view, so a dict merge cannot
    overwrite an inherited value — it has to be removed from the base.
    """

    def test_every_name_the_identity_can_set_is_declared_run_owned(self, tmp_path):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host x")
        (ssh / "known_hosts").write_text("x")
        produced = set(ssh_run_env(
            ResolvedSshIdentity(private_key=_PEM), tmp_path, ttl_seconds=60,
        ))
        assert produced <= RUN_OWNED_SSH_ENV_VARS
        # The socket/pid the per-run ssh-agent exports are owned too, even though no
        # Python helper sets them: an inherited SSH_AUTH_SOCK is a peer's live agent.
        assert {"SSH_AUTH_SOCK", "SSH_AGENT_PID"} <= RUN_OWNED_SSH_ENV_VARS

    def test_inherited_values_are_stripped_and_unrelated_ones_kept(self):
        base = {
            "AGENTO_SSH_PRIVATE_KEY": "peer-key",
            "SSH_AUTH_SOCK": "/tmp/peer.sock",
            "GIT_SSH_COMMAND": "ssh -i /peer/id_rsa",
            "AGENTO_SSH_TTL": "999",
            "SSH_AGENT_PID": "1",
            "PATH": "/usr/bin",
            "GIT_AUTHOR_NAME": "keep me",
        }
        assert without_run_owned_ssh_env(base) == {
            "PATH": "/usr/bin", "GIT_AUTHOR_NAME": "keep me",
        }

    def test_an_identity_less_run_contributes_nothing(self, tmp_path):
        assert ssh_run_env(ResolvedSshIdentity(), tmp_path, ttl_seconds=60) == {}


class TestGitSshCommandEnv:
    def test_empty_without_a_private_key(self, tmp_path):
        assert git_ssh_command_env(tmp_path, has_private_key=False) == {}

    def test_bare_ssh_when_no_optional_files_exist(self, tmp_path):
        env = git_ssh_command_env(tmp_path, has_private_key=True)
        assert env == {"GIT_SSH_COMMAND": "ssh -o IdentitiesOnly=no"}

    def test_includes_each_option_only_when_its_file_exists(self, tmp_path):
        ssh = tmp_path / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host x")
        env = git_ssh_command_env(tmp_path, has_private_key=True)
        assert env["GIT_SSH_COMMAND"] == (
            f"ssh -F {ssh / 'config'} -o IdentitiesOnly=no"
        )

        (ssh / "known_hosts").write_text("x")
        env = git_ssh_command_env(tmp_path, has_private_key=True)
        assert env["GIT_SSH_COMMAND"] == (
            f"ssh -F {ssh / 'config'} -o UserKnownHostsFile={ssh / 'known_hosts'} "
            "-o IdentitiesOnly=no"
        )


class TestResolveSshIdentity:
    def test_reads_exactly_the_four_config_paths(self):
        svc = _Svc({SSH_PRIVATE_KEY_PATH: _PEM, SSH_PUBLIC_KEY_PATH: "pub"})
        resolved = resolve_ssh_identity(svc)
        assert svc.calls == [
            SSH_PRIVATE_KEY_PATH, SSH_PUBLIC_KEY_PATH,
            SSH_CONFIG_PATH, SSH_KNOWN_HOSTS_PATH,
        ]
        assert resolved.private_key == _PEM
        assert resolved.public_key == "pub"
        assert resolved.config == ""

    def test_none_service_is_all_empty(self):
        assert resolve_ssh_identity(None) == ResolvedSshIdentity()

    @pytest.mark.parametrize("size", [
        MAX_SSH_PRIVATE_KEY_BYTES - 1, MAX_SSH_PRIVATE_KEY_BYTES,
    ])
    def test_a_key_at_or_below_the_limit_resolves(self, size):
        svc = _Svc({SSH_PRIVATE_KEY_PATH: "k" * size})
        assert len(resolve_ssh_identity(svc).private_key) == size

    def test_one_byte_over_the_limit_fails_closed(self):
        svc = _Svc({SSH_PRIVATE_KEY_PATH: "k" * (MAX_SSH_PRIVATE_KEY_BYTES + 1)})
        with pytest.raises(ValueError) as exc:
            resolve_ssh_identity(svc)
        message = str(exc.value)
        assert SSH_PRIVATE_KEY_PATH in message
        assert str(MAX_SSH_PRIVATE_KEY_BYTES) in message
        assert str(MAX_SSH_PRIVATE_KEY_BYTES + 1) in message

    def test_the_limit_is_bytes_not_characters(self):
        """A multibyte value whose char count fits but whose byte count does not."""
        value = "é" * (MAX_SSH_PRIVATE_KEY_BYTES // 2 + 1)  # 2 bytes each
        assert len(value) <= MAX_SSH_PRIVATE_KEY_BYTES
        with pytest.raises(ValueError):
            resolve_ssh_identity(_Svc({SSH_PRIVATE_KEY_PATH: value}))

    def test_system_json_declares_the_same_limit(self):
        """Two limits that can drift are one limit that does not hold."""
        system = json.loads(
            (_REPO / "src/agento/modules/agent_view/system.json").read_text()
        )
        assert system["identity/ssh_private_key"]["maxLength"] == MAX_SSH_PRIVATE_KEY_BYTES


class TestRedactedRepr:
    def test_repr_and_str_hide_the_key_but_keep_the_rest(self):
        resolved = ResolvedSshIdentity(
            private_key=_PEM, public_key="ssh-ed25519 VISIBLE",
        )
        for rendered in (repr(resolved), f"{resolved}", str(resolved)):
            assert "BEGIN" not in rendered
            assert "b3BlbnNz" not in rendered
            assert "VISIBLE" in rendered


class TestSshRunEnv:
    def test_empty_without_a_private_key(self, tmp_path):
        assert ssh_run_env(ResolvedSshIdentity(), tmp_path, ttl_seconds=60) == {}

    def test_carries_key_git_command_and_ttl(self, tmp_path):
        env = ssh_run_env(
            ResolvedSshIdentity(private_key=_PEM), tmp_path, ttl_seconds=1200,
        )
        assert env["AGENTO_SSH_PRIVATE_KEY"] == _PEM
        assert env["GIT_SSH_COMMAND"] == "ssh -o IdentitiesOnly=no"
        assert env["AGENTO_SSH_TTL"] == "1200"

    def test_the_interactive_ttl_is_a_named_constant(self, tmp_path):
        env = ssh_run_env(
            ResolvedSshIdentity(private_key=_PEM), tmp_path,
            ttl_seconds=SSH_TTL_LOCAL_RUN,
        )
        assert env["AGENTO_SSH_TTL"] == "43200"

    def test_takes_a_resolved_identity_not_a_config_service(self):
        """A service passed where the dataclass belongs must be a type error, not a
        silent second read of the four config values."""
        with pytest.raises(AttributeError):
            ssh_run_env(_Svc({SSH_PRIVATE_KEY_PATH: _PEM}), None, ttl_seconds=60)
