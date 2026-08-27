"""config:set must refuse a private-key value that does not parse."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agento.framework.cli.config import _validate_config_value

FIELD = "identity/ssh_private_key"
PATH = f"agent_view/{FIELD}"


@pytest.fixture(autouse=True)
def module_on_disk(tmp_path, monkeypatch):
    d = tmp_path / "agent_view"
    d.mkdir()
    (d / "system.json").write_text(json.dumps({
        FIELD: {"type": "obscure", "label": "SSH private key"},
        "identity/ssh_public_key": {"type": "textarea", "label": "SSH public key"},
        "some_secret": {"type": "obscure", "label": "Other secret"},
    }))
    monkeypatch.setattr(
        "agento.framework.core_config._find_module_dir",
        lambda name: d if name == "agent_view" else None,
    )
    return d


@pytest.fixture(scope="module")
def real_private_key(tmp_path_factory):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    p = tmp_path_factory.mktemp("k") / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(p)],
        check=True, capture_output=True,
    )
    return Path(p).read_text()


def test_a_real_key_is_accepted(real_private_key):
    assert _validate_config_value(PATH, real_private_key) is True


def test_the_incident_value_is_rejected(capsys):
    assert _validate_config_value(PATH, "-----BEGIN OPENSSH PRIVATE KEY-----") is False
    out = capsys.readouterr().out
    assert "does not parse" in out
    assert "< id_rsa" in out       # the fix is on screen


def test_junk_is_rejected():
    assert _validate_config_value(PATH, "hello") is False


def test_an_empty_value_is_allowed(real_private_key):
    """Clearing the field must stay possible."""
    assert _validate_config_value(PATH, "") is True


def test_an_encrypted_key_is_rejected_with_its_own_message(tmp_path, capsys):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    p = tmp_path / "enc"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "hunter2", "-f", str(p)],
        check=True, capture_output=True,
    )
    assert _validate_config_value(PATH, p.read_text()) is False
    assert "passphrase" in capsys.readouterr().out


def test_the_rejection_message_never_echoes_the_value(real_private_key, capsys):
    _validate_config_value(PATH, real_private_key[:60])
    out = capsys.readouterr().out
    assert real_private_key[30:60] not in out


def test_an_unrelated_obscure_field_is_untouched():
    assert _validate_config_value("agent_view/some_secret", "anything at all") is True


def test_a_public_key_field_is_untouched():
    assert _validate_config_value("agent_view/identity/ssh_public_key", "junk") is True
