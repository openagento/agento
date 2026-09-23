"""`resolve_ssh_identity` warns loudly about a key that does not parse — and still resolves.

The diagnostic used to live in `workspace_build.builder.materialize_ssh_identity`, which
wrote the key to `<build>/.ssh/id_rsa`. That function is gone (D-SSH-1: the private key is
never a file), so the same check now runs where the key is read instead — once per run, on
the single path both spawn paths use. The assertions that the key "is still written" are
therefore inverted: nothing may write it anywhere.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from agento.framework.ssh_identity import SSH_PRIVATE_KEY_PATH, resolve_ssh_identity


class _Svc:
    def __init__(self, private):
        self.private = private

    def get(self, path):
        return self.private if path == SSH_PRIVATE_KEY_PATH else ""


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    """A real ed25519 keypair, generated once.
    ssh-keygen is present in CI and in the cron image; skip rather than fail."""
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    d = tmp_path_factory.mktemp("keys")
    path = d / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "agent@agento", "-f", str(path)],
        check=True, capture_output=True,
    )
    return path.read_text(), (d / "id_ed25519.pub").read_text().strip()


@pytest.fixture(scope="module")
def encrypted_private(tmp_path_factory):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    path = tmp_path_factory.mktemp("enc") / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "hunter2", "-f", str(path)],
        check=True, capture_output=True,
    )
    return path.read_text()


def test_truncated_key_warns_and_still_resolves(caplog):
    """Never fatal: the run continues and the prelude reports the missing identity."""
    with caplog.at_level(logging.WARNING):
        resolved = resolve_ssh_identity(_Svc("-----BEGIN OPENSSH PRIVATE KEY-----"))
    assert resolved.private_key == "-----BEGIN OPENSSH PRIVATE KEY-----"
    assert any("does not parse" in r.getMessage() for r in caplog.records)
    assert any("identity:check" in r.getMessage() for r in caplog.records)


def test_a_key_of_the_right_shape_and_the_wrong_bytes_still_warns(caplog):
    """The reason this parses rather than pattern-matching: a correctly enveloped,
    plausibly sized body that is not a key is exactly the paste this incident
    produced, and a structural check waves it through."""
    body = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + "b3BlbnNzaC1rZXktdjEAAAAA" * 12 + "\n"
        + "-----END OPENSSH PRIVATE KEY-----\n"
    )
    with caplog.at_level(logging.WARNING):
        resolve_ssh_identity(_Svc(body))
    assert any("does not parse" in r.getMessage() for r in caplog.records)


def test_a_real_key_does_not_warn(caplog, keypair):
    private, _ = keypair
    with caplog.at_level(logging.WARNING):
        resolve_ssh_identity(_Svc(private))
    assert [r for r in caplog.records if "does not parse" in r.getMessage()] == []


def test_a_passphrase_protected_key_says_so(caplog, encrypted_private):
    # Distinct message: "wrong bytes" and "cannot be used unattended" are
    # different operator actions.
    with caplog.at_level(logging.WARNING):
        resolve_ssh_identity(_Svc(encrypted_private))
    assert any("passphrase-protected" in r.getMessage() for r in caplog.records)


def test_no_warning_message_contains_key_material(caplog):
    with caplog.at_level(logging.WARNING):
        resolve_ssh_identity(_Svc("-----BEGIN OPENSSH PRIVATE KEY-----"))
    for record in caplog.records:
        assert "PRIVATE KEY-----" not in record.getMessage()


def test_an_empty_key_is_not_diagnosed(caplog):
    """No key configured is not a malformed key — silence, not a warning."""
    with caplog.at_level(logging.WARNING):
        resolve_ssh_identity(_Svc(""))
    assert [r for r in caplog.records if "resolve_ssh_identity" in r.getMessage()] == []
