"""SSH key parsing / derivation / pair checking — no shelling out to ssh-keygen."""
from __future__ import annotations

import base64
import hashlib
import subprocess

import pytest

from agento.framework.ssh_keys import (
    CODE_ENCRYPTED,
    CODE_INVALID_KEY,
    CODE_NO_PUBLIC,
    CODE_NOT_SET,
    CODE_OK,
    CODE_PAIR_MISMATCH,
    EncryptedKeyError,
    check_keypair,
    derive_public_key,
    openssh_fingerprint,
)

GARBAGE_HEADER_ONLY = "-----BEGIN OPENSSH PRIVATE KEY-----"


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    """A real ed25519 keypair, generated once. ssh-keygen is present in CI and
    in the cron image; skip rather than fail if it is not."""
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    d = tmp_path_factory.mktemp("keys")
    p = d / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "agent@agento", "-f", str(p)],
        check=True, capture_output=True,
    )
    return p.read_text(), (d / "id_ed25519.pub").read_text().strip()


@pytest.fixture(scope="module")
def other_public(tmp_path_factory):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    d = tmp_path_factory.mktemp("keys2")
    p = d / "other"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "other", "-f", str(p)],
        check=True, capture_output=True,
    )
    return (d / "other.pub").read_text().strip()


# --- derive_public_key -----------------------------------------------------

def test_derive_matches_the_generated_public_key_ignoring_the_comment(keypair):
    private, public = keypair
    derived = derive_public_key(private)
    assert derived.split() == public.split()[:2]
    assert "agent@agento" not in derived     # the comment is not invented


def test_derive_rejects_the_header_only_value_from_the_incident(keypair):
    with pytest.raises(ValueError):
        derive_public_key(GARBAGE_HEADER_ONLY)


@pytest.mark.parametrize("bad", ["", "   ", "not a key at all", "ssh-ed25519 AAAA"])
def test_derive_rejects_junk(bad):
    with pytest.raises(ValueError):
        derive_public_key(bad)


def test_derive_reports_an_encrypted_key_distinctly(tmp_path):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    p = tmp_path / "enc"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "hunter2", "-f", str(p)],
        check=True, capture_output=True,
    )
    with pytest.raises(EncryptedKeyError):
        derive_public_key(p.read_text())


# --- openssh_fingerprint ---------------------------------------------------

def test_fingerprint_matches_ssh_keygen(keypair, tmp_path):
    _, public = keypair
    pub_file = tmp_path / "k.pub"
    pub_file.write_text(public + "\n")
    expected = subprocess.run(
        ["ssh-keygen", "-lf", str(pub_file)], capture_output=True, text=True, check=True,
    ).stdout.split()[1]
    assert openssh_fingerprint(public) == expected


def test_fingerprint_is_computed_from_the_blob_not_the_text(keypair):
    _, public = keypair
    blob = base64.b64decode(public.split()[1])
    want = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert openssh_fingerprint(public) == want
    # Adding a comment must not change the fingerprint.
    assert openssh_fingerprint(public + " extra-comment") == want


def test_fingerprint_raises_on_junk():
    with pytest.raises(ValueError):
        openssh_fingerprint("not-a-public-key")


# --- check_keypair ---------------------------------------------------------

def test_matching_pair_is_ok(keypair):
    private, public = keypair
    check = check_keypair(private, public)
    assert check.code == CODE_OK
    assert "SHA256:" in check.detail


def test_mismatched_pair_is_reported(keypair, other_public):
    private, _ = keypair
    assert check_keypair(private, other_public).code == CODE_PAIR_MISMATCH


def test_the_incident_value_is_invalid_key(keypair):
    _, public = keypair
    check = check_keypair(GARBAGE_HEADER_ONLY, public)
    assert check.code == CODE_INVALID_KEY


def test_no_private_key_is_not_set(keypair):
    _, public = keypair
    assert check_keypair(None, public).code == CODE_NOT_SET
    assert check_keypair("   ", public).code == CODE_NOT_SET


def test_private_key_without_a_public_key_is_reported(keypair):
    private, _ = keypair
    check = check_keypair(private, None)
    assert check.code == CODE_NO_PUBLIC
    assert "SHA256:" in check.detail   # still useful: the real fingerprint


def test_encrypted_private_key_is_reported(tmp_path, keypair):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    _, public = keypair
    p = tmp_path / "enc"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "hunter2", "-f", str(p)],
        check=True, capture_output=True,
    )
    assert check_keypair(p.read_text(), public).code == CODE_ENCRYPTED


def test_a_public_key_with_a_different_comment_still_matches(keypair):
    private, public = keypair
    parts = public.split()
    assert check_keypair(private, f"{parts[0]} {parts[1]} someone-else@host").code == CODE_OK


def test_no_check_result_ever_contains_private_key_material(keypair, other_public):
    private, public = keypair
    body = "".join(private.split())
    for check in (
        check_keypair(private, public),
        check_keypair(private, other_public),
        check_keypair(private, None),
        check_keypair(GARBAGE_HEADER_ONLY, public),
    ):
        assert check.detail
        for line in private.splitlines():
            stripped = line.strip()
            if len(stripped) >= 20 and "PRIVATE KEY" not in stripped:
                assert stripped not in check.detail
        assert body[:40] not in check.detail


# --- PEM (non-OpenSSH) private keys ---------------------------------------
# `load_ssh_private_key` raises "Not OpenSSH private key format" on these, so a
# single-loader implementation would report INVALID_KEY for a valid key and
# Task 6 would refuse to store it. Both formats must round-trip.

@pytest.fixture
def pem_keypair():
    """A traditional-PEM RSA private key plus its OpenSSH public line."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(
        Encoding.OpenSSH, PublicFormat.OpenSSH
    ).decode()
    return private, public


def test_derive_public_key_accepts_a_traditional_pem_key(pem_keypair):
    private, public = pem_keypair
    assert derive_public_key(private).split()[:2] == public.split()[:2]


def test_check_keypair_accepts_a_traditional_pem_pair(pem_keypair):
    private, public = pem_keypair
    assert check_keypair(private, public).code == CODE_OK


def test_check_keypair_detects_a_mismatched_pem_pair(pem_keypair, other_public):
    private, _ = pem_keypair
    assert check_keypair(private, other_public).code == CODE_PAIR_MISMATCH


def test_pkcs8_pem_is_also_accepted():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(
        Encoding.OpenSSH, PublicFormat.OpenSSH
    ).decode()
    assert check_keypair(private, public).code == CODE_OK


def test_an_encrypted_pem_key_is_reported_as_encrypted_not_invalid():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import (
        BestAvailableEncryption,
        Encoding,
        PrivateFormat,
    )

    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(b"hunter2")
    ).decode()
    assert check_keypair(private, None).code == CODE_ENCRYPTED
