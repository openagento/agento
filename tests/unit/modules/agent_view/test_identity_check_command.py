"""agent_view:identity:check — CLI surface over the same keypair check."""
from __future__ import annotations

import argparse
import subprocess
from types import SimpleNamespace

import pytest

from agento.modules.agent_view.src.commands.identity_check import IdentityCheckCommand

PRIVATE = "agent_view/identity/ssh_private_key"
PUBLIC = "agent_view/identity/ssh_public_key"


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    if subprocess.run(["which", "ssh-keygen"], capture_output=True).returncode != 0:
        pytest.skip("ssh-keygen not available")
    d = tmp_path_factory.mktemp("k")
    p = d / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(p)],
        check=True, capture_output=True,
    )
    return p.read_text(), (d / "id_ed25519.pub").read_text().strip()


@pytest.fixture
def wired(monkeypatch):
    """Stub the one resolution seam. The values arrive already resolved through
    the ENV -> DB -> config.json fallback, so the test hands over plaintext."""
    module = "agento.modules.agent_view.src.commands.identity_check"

    def _install(private, public, *, view=None):
        view = view or SimpleNamespace(id=7, code="dev", workspace_id=1)
        monkeypatch.setattr(
            f"{module}._resolve_identity",
            lambda code: (view, private, public),
        )

    return _install


def test_ok_prints_ok_and_exits_zero(keypair, wired, capsys):
    private, public = keypair
    wired(private, public)
    with pytest.raises(SystemExit) as exc:
        IdentityCheckCommand().execute(argparse.Namespace(agent_view_code="dev"))
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "SHA256:" in out


def test_the_incident_value_prints_invalid_key_and_exits_one(keypair, wired, capsys):
    _, public = keypair
    wired("-----BEGIN OPENSSH PRIVATE KEY-----", public)
    with pytest.raises(SystemExit) as exc:
        IdentityCheckCommand().execute(argparse.Namespace(agent_view_code="dev"))
    assert exc.value.code == 1
    assert "INVALID_KEY" in capsys.readouterr().out


def test_nothing_stored_prints_not_set_and_exits_one(wired, capsys):
    wired(None, None)
    with pytest.raises(SystemExit) as exc:
        IdentityCheckCommand().execute(argparse.Namespace(agent_view_code="dev"))
    assert exc.value.code == 1
    assert "NOT_SET" in capsys.readouterr().out


def test_output_never_contains_key_material(keypair, wired, capsys):
    private, public = keypair
    wired(private, public)
    with pytest.raises(SystemExit):
        IdentityCheckCommand().execute(argparse.Namespace(agent_view_code="dev"))
    out = capsys.readouterr().out
    for line in private.splitlines():
        stripped = line.strip()
        if len(stripped) >= 20 and "PRIVATE KEY" not in stripped:
            assert stripped not in out


def test_a_value_that_cannot_be_decrypted_is_reported(capsys, monkeypatch):
    """`get(..., strict=True)` raises DecryptError when the ciphertext was
    written under a different AGENTO_ENCRYPTION_KEY. That is its own outcome, not
    INVALID_KEY and — crucially — not NOT_SET: the lenient default falls back to
    config.json, which is how an unreadable key came to look like an absent one.
    """
    from agento.framework.config_resolver import DecryptError

    module = "agento.modules.agent_view.src.commands.identity_check"

    def _boom(code):
        raise DecryptError("agent_view/identity/ssh_private_key")

    monkeypatch.setattr(f"{module}._resolve_identity", _boom)
    with pytest.raises(SystemExit) as exc:
        IdentityCheckCommand().execute(argparse.Namespace(agent_view_code="dev"))
    assert exc.value.code == 1
    assert "decrypt" in capsys.readouterr().out.lower()


def test_a_key_supplied_only_by_env_var_is_found(keypair, monkeypatch, capsys):
    """Guard for the resolution class: the checker must see every level of the
    config fallback, not just DB rows. A DB-only resolver reports NOT_SET here
    while workspace_build materializes the key perfectly well."""
    private, public = keypair
    module = "agento.modules.agent_view.src.commands.identity_check"
    monkeypatch.setenv("CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY", private)
    monkeypatch.setenv("CONFIG__AGENT_VIEW__IDENTITY__SSH_PUBLIC_KEY", public)

    from agento.framework.config_resolver import ScopedConfigService

    view = SimpleNamespace(id=7, code="dev", workspace_id=1)
    monkeypatch.setattr(
        f"{module}._load_framework_config", lambda: ({}, None, None), raising=False
    )
    # Exercise the real service against an empty DB: every value must come from
    # the environment level.
    # Patched where it is DEFINED: `ScopedConfigService.__init__` imports it from
    # `.scoped_config` at call time, so the name never exists on config_resolver.
    monkeypatch.setattr(
        "agento.framework.scoped_config.build_scoped_overrides",
        lambda *a, **k: {},
    )
    svc = ScopedConfigService(None, "agent_view", view.id, workspace_id=view.workspace_id)
    assert svc.get("agent_view/identity/ssh_private_key") == private


def test_command_metadata():
    cmd = IdentityCheckCommand()
    assert cmd.name == "agent_view:identity:check"
    assert cmd.shortcut == "av:id:ch"
    parser = argparse.ArgumentParser()
    cmd.configure(parser)
    assert parser.parse_args(["dev"]).agent_view_code == "dev"
