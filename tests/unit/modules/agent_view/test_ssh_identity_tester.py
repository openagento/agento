"""agent_view SSH identity: the pure check, and the local tester around it."""
from __future__ import annotations

import subprocess

import pytest

from agento.framework.config_test import ERROR, FAIL, NOT_CONFIGURED, OK
from agento.modules.agent_view.src.testers.ssh_identity import (
    PRIVATE_PATH,
    PUBLIC_PATH,
    SshIdentityTester,
    check_identity,
)

GARBAGE_HEADER_ONLY = "-----BEGIN OPENSSH PRIVATE KEY-----"


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    d = tmp_path_factory.mktemp("k")
    p = d / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "agent@agento", "-f", str(p)],
        check=True, capture_output=True,
    )
    return p.read_text(), (d / "id_ed25519.pub").read_text().strip()


# --- check_identity: the pure mapping ---------------------------------------

def test_matching_pair_is_ok(keypair):
    private, public = keypair
    result = check_identity(private, public)
    assert result.status == OK
    assert result.code == "OK"
    assert "SHA256:" in result.message


def test_nothing_stored_is_not_configured():
    result = check_identity(None, None)
    assert result.status == NOT_CONFIGURED
    assert result.code == "NOT_SET"


def test_the_incident_value_fails_with_invalid_key(keypair):
    _, public = keypair
    result = check_identity(GARBAGE_HEADER_ONLY, public)
    assert result.status == FAIL
    assert result.code == "INVALID_KEY"
    assert "config:set" in result.message      # tells the operator how to fix it


def test_mismatched_pair_fails(keypair, tmp_path):
    private, _ = keypair
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    other = tmp_path / "other"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(other)],
        check=True, capture_output=True,
    )
    result = check_identity(private, (tmp_path / "other.pub").read_text().strip())
    assert result.status == FAIL
    assert result.code == "PAIR_MISMATCH"


def test_message_is_a_single_line_without_key_material(keypair):
    private, public = keypair
    for args in ((private, public), (private, None), (GARBAGE_HEADER_ONLY, public)):
        message = check_identity(*args).message
        assert "\n" not in message
        for line in private.splitlines():
            stripped = line.strip()
            if len(stripped) >= 20 and "PRIVATE KEY" not in stripped:
                assert stripped not in message


# --- SshIdentityTester: the resolution around it ----------------------------

class _Svc:
    """Stand-in for ScopedConfigService. `strict` is not optional here: the
    tester must pass it for the private key, and a stub without the keyword would
    make these tests fail with TypeError instead of asserting behaviour."""

    def __init__(self, values, raises=()):
        self.values = values
        self.raises = set(raises)
        self.asked: list[tuple[str, bool]] = []

    def get(self, path, *, strict=False):
        self.asked.append((path, strict))
        if strict and path in self.raises:
            from agento.framework.config_resolver import DecryptError

            raise DecryptError(path)
        return self.values.get(path)


@pytest.fixture
def service(monkeypatch):
    def _install(values, raises=()):
        svc = _Svc(values, raises)
        monkeypatch.setattr(
            "agento.modules.agent_view.src.testers.ssh_identity.ScopedConfigService",
            lambda conn, scope, scope_id: svc,
        )
        return svc

    return _install


def test_the_tester_resolves_exactly_its_two_paths(keypair, service):
    private, public = keypair
    svc = service({PRIVATE_PATH: private, PUBLIC_PATH: public})
    result = SshIdentityTester().run(None, scope="agent_view", scope_id=7)
    assert result.status == OK
    # Two per-path reads and nothing else — never resolve_all(), which would
    # decrypt every module's secrets to test one field.
    assert [p for p, _ in svc.asked] == [PRIVATE_PATH, PUBLIC_PATH]


def test_the_private_key_is_read_strictly(keypair, service):
    """strict=True is what turns "stored but unreadable" into ERROR instead of
    NOT_SET. The public key is not encrypted, so it is read leniently."""
    private, public = keypair
    svc = service({PRIVATE_PATH: private, PUBLIC_PATH: public})
    SshIdentityTester().run(None, scope="agent_view", scope_id=7)
    assert dict(svc.asked) == {PRIVATE_PATH: True, PUBLIC_PATH: False}


def test_an_undecryptable_key_is_an_error_not_not_configured(service):
    service({}, raises={PRIVATE_PATH})
    result = SshIdentityTester().run(None, scope="agent_view", scope_id=7)
    assert result.status == ERROR
    assert result.code == "DECRYPT_FAILED"
    assert "AGENTO_ENCRYPTION_KEY" in result.message


def test_a_resolution_failure_is_an_error_with_no_message_text(service, monkeypatch):
    """A resolver exception can quote the value it was handling, so only the
    exception TYPE crosses into the result."""
    monkeypatch.setattr(
        "agento.modules.agent_view.src.testers.ssh_identity.ScopedConfigService",
        lambda conn, scope, scope_id: (_ for _ in ()).throw(RuntimeError("secret-ish")),
    )
    result = SshIdentityTester().run(None, scope="agent_view", scope_id=7)
    assert result.status == ERROR
    assert result.code == "READ_FAILED"
    assert "secret-ish" not in result.message
    assert "RuntimeError" in result.message


def test_nothing_stored_reads_as_not_configured(service):
    service({PRIVATE_PATH: None, PUBLIC_PATH: None})
    assert SshIdentityTester().run(None, scope="default", scope_id=0).code == "NOT_SET"
