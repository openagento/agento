"""materialize_ssh_identity warns loudly about a key that does not parse — and still writes it."""
from __future__ import annotations

import logging
import subprocess

import pytest

from agento.modules.workspace_build.src.builder import materialize_ssh_identity

PRIVATE = "agent_view/identity/ssh_private_key"


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    """A real ed25519 keypair, generated once — same pattern as Task 4's tests.
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


def test_truncated_key_warns_and_is_still_written(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        materialize_ssh_identity(
            tmp_path, {PRIVATE: "-----BEGIN OPENSSH PRIVATE KEY-----"}
        )
    key = tmp_path / ".ssh" / "id_rsa"
    assert key.is_file()                                  # behaviour unchanged
    assert oct(key.stat().st_mode)[-3:] == "600"
    assert any("does not parse" in r.getMessage() for r in caplog.records)
    assert any("identity:check" in r.getMessage() for r in caplog.records)


def test_a_key_of_the_right_shape_and_the_wrong_bytes_still_warns(tmp_path, caplog):
    """The reason this parses rather than pattern-matching: a correctly enveloped,
    plausibly sized body that is not a key is exactly the paste this incident
    produced, and a structural check waves it through."""
    body = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + "b3BlbnNzaC1rZXktdjEAAAAA" * 12 + "\n"
        + "-----END OPENSSH PRIVATE KEY-----\n"
    )
    with caplog.at_level(logging.WARNING):
        materialize_ssh_identity(tmp_path, {PRIVATE: body})
    assert any("does not parse" in r.getMessage() for r in caplog.records)
    assert (tmp_path / ".ssh" / "id_rsa").is_file()


def test_a_real_key_does_not_warn(tmp_path, caplog, keypair):
    private, _ = keypair
    with caplog.at_level(logging.WARNING):
        materialize_ssh_identity(tmp_path, {PRIVATE: private})
    assert [r for r in caplog.records if "does not parse" in r.getMessage()] == []


def test_a_passphrase_protected_key_says_so(tmp_path, caplog, encrypted_private):
    # Distinct message: "wrong bytes" and "cannot be used unattended" are
    # different operator actions.
    private = encrypted_private
    with caplog.at_level(logging.WARNING):
        materialize_ssh_identity(tmp_path, {PRIVATE: private})
    assert any("passphrase-protected" in r.getMessage() for r in caplog.records)


def test_no_warning_message_contains_key_material(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        materialize_ssh_identity(tmp_path, {PRIVATE: "-----BEGIN OPENSSH PRIVATE KEY-----"})
    for record in caplog.records:
        assert "PRIVATE KEY-----" not in record.getMessage()
