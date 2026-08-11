"""Regression tests for impl review round 3.

Theme: the harness registry was open, but three *consumers* of it were not — the install
wizard, dynamic config options, and the dev Docker image all still assumed the two shipped
harnesses.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.harness import clear
from tests.harness_fixtures import register_builtin_harnesses

REPO = Path(__file__).resolve().parents[4]


def _seed_local_harness(root: Path, name: str = "hermes") -> Path:
    """A harness module under ``app/code`` — the deployment-extension shape."""
    mod = root / "app" / "code" / name
    mod.mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
    (mod / "di.json").write_text(json.dumps({"agent_harnesses": [{
        "id": name,
        "label": name.title(),
        "class": "src.adapter.Adapter",
        "default_provider": "cloud",
        "providers": [
            {"id": "local", "label": "Local", "credential_required": False},
            {"id": "cloud", "label": "Cloud", "credential_required": True,
             "credential_scope": f"{name}_cloud", "registration_modes": ["api_key"]},
        ],
    }]}))
    return root


class TestInstallWizardIsDescriptorDriven:
    """`_setup_agent_provider` imported `get_available_providers`, which the refactor
    removed — so a fresh `agento install` crashed right after `setup:upgrade`."""

    def test_the_removed_symbol_is_gone_and_not_imported(self):
        from agento.framework.agent_manager import auth
        from agento.framework.cli import install

        assert not hasattr(auth, "get_available_providers")
        assert "get_available_providers" not in Path(install.__file__).read_text()

    def test_options_are_harness_provider_pairs_from_the_declarations(self, tmp_path):
        from agento.framework.cli.install import _setup_agent_harness

        proj = _seed_local_harness(tmp_path)
        captured: dict = {}

        def _select(prompt, options):
            captured["prompt"] = prompt
            captured["options"] = options
            return len(options) - 1  # choose "Skip"

        with patch("agento.framework.cli.terminal.select", _select):
            _setup_agent_harness(["docker", "compose"], proj)

        # Every (harness, provider) pair, including the app/code one.
        joined = " | ".join(captured["options"])
        assert "Claude Code / Anthropic" in joined
        assert "OpenAI Codex / OpenAI" in joined
        assert "Hermes / Cloud" in joined
        # A credential-less provider is labelled as such so the operator knows why no
        # registration prompt follows.
        assert "Hermes / Local (no credential needed)" in joined

    def test_skip_registers_nothing(self, tmp_path):
        from agento.framework.cli.install import _setup_agent_harness

        proj = _seed_local_harness(tmp_path)
        with (
            patch("agento.framework.cli.terminal.select", lambda p, o: len(o) - 1),
            patch("agento.framework.cli.install.subprocess.run") as run,
        ):
            _setup_agent_harness(["docker", "compose"], proj)
        run.assert_not_called()

    def test_credential_required_provider_registers_its_scope_and_binds_both_paths(
        self, tmp_path,
    ):
        from agento.framework.cli.install import _setup_agent_harness

        proj = _seed_local_harness(tmp_path)

        def _select(prompt, options):
            return next(i for i, o in enumerate(options) if o.startswith("Hermes / Cloud"))

        with (
            patch("agento.framework.cli.terminal.select", _select),
            patch("agento.framework.cli.install.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0)
            _setup_agent_harness(["docker", "compose"], proj)

        argvs = [" ".join(c.args[0]) for c in run.call_args_list]
        # Registers the SCOPE (not the harness id) — they are independent axes.
        assert any("credential:register hermes_cloud default" in a for a in argvs)
        assert any("config:set agent_view/harness hermes" in a for a in argvs)
        assert any("config:set agent_view/provider cloud" in a for a in argvs)

    def test_credential_less_provider_skips_registration_entirely(self, tmp_path):
        from agento.framework.cli.install import _setup_agent_harness

        proj = _seed_local_harness(tmp_path)

        def _select(prompt, options):
            return next(i for i, o in enumerate(options) if o.startswith("Hermes / Local"))

        with (
            patch("agento.framework.cli.terminal.select", _select),
            patch("agento.framework.cli.install.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=0)
            _setup_agent_harness(["docker", "compose"], proj)

        argvs = [" ".join(c.args[0]) for c in run.call_args_list]
        assert not any("credential:register" in a for a in argvs)
        assert any("config:set agent_view/provider local" in a for a in argvs)

    def test_failed_registration_does_not_bind_config(self, tmp_path):
        """Binding a harness whose credential never registered would leave a view that
        cannot run."""
        from agento.framework.cli.install import _setup_agent_harness

        proj = _seed_local_harness(tmp_path)

        def _select(prompt, options):
            return next(i for i, o in enumerate(options) if o.startswith("Hermes / Cloud"))

        with (
            patch("agento.framework.cli.terminal.select", _select),
            patch("agento.framework.cli.install.subprocess.run") as run,
        ):
            run.return_value = MagicMock(returncode=1)  # registration fails
            _setup_agent_harness(["docker", "compose"], proj)

        argvs = [" ".join(c.args[0]) for c in run.call_args_list]
        assert not any("config:set" in a for a in argvs)


class TestDynamicOptionsSeeDeploymentModules:
    """`resolve_options()` was called with no project root, and `iter_module_dirs(None)`
    deliberately yields CORE modules only — so an app/code or PyPI harness could never be
    selected in `config:set`, `config:schema` or the admin TUI. That is the central
    "third harness with no framework edits" promise failing operationally."""

    def test_module_root_is_resolved_from_the_ambient_project(self):
        from agento.framework.module_discovery import resolve_module_root

        root = resolve_module_root()
        assert root is not None
        assert (root / "src" / "agento").is_dir() or (root / "app" / "code").is_dir()

    def test_container_layout_resolves_to_the_root_that_exposes_app_code(self, tmp_path):
        """Inside the cron container there is no project dir — app/code is bind-mounted at
        /app/code, so the root must be its grandparent."""
        from agento.framework import module_discovery

        fake_app_code = tmp_path / "app" / "code"
        fake_app_code.mkdir(parents=True)
        # Round 10 moved find_project_root into the framework layer (framework/project.py)
        # so framework modules no longer import from framework/cli/.
        with (
            patch("agento.framework.project.find_project_root", return_value=None),
            patch("agento.framework.bootstrap.USER_MODULES_DIR", str(fake_app_code)),
        ):
            assert module_discovery.resolve_module_root() == tmp_path

    def test_no_project_and_no_app_code_stays_core_only(self, tmp_path):
        """A fresh install has neither — core-only is the correct answer there."""
        from agento.framework import module_discovery

        with (
            patch("agento.framework.project.find_project_root", return_value=None),
            patch("agento.framework.bootstrap.USER_MODULES_DIR", str(tmp_path / "nope")),
        ):
            assert module_discovery.resolve_module_root() is None

    def test_local_harness_is_offered_by_resolve_options(self, tmp_path):
        from agento.framework.harness import resolve_options

        proj = _seed_local_harness(tmp_path)
        with patch(
            "agento.framework.module_discovery.resolve_module_root", return_value=proj,
        ):
            values = {o["value"] for o in resolve_options("agent_harness_registry")}

        assert "hermes" in values, "an app/code harness must be selectable"

    def test_config_set_accepts_a_local_harness(self, tmp_path):
        """End-to-end through the real validation path, which is what actually gates it."""
        from agento.framework.cli.config import _validate_config_value
        from agento.framework.scoped_config import Scope

        proj = _seed_local_harness(tmp_path)
        with patch(
            "agento.framework.module_discovery.resolve_module_root", return_value=proj,
        ):
            assert _validate_config_value(
                "agent_view/harness", "hermes",
                conn=MagicMock(), scope=Scope.DEFAULT, scope_id=0,
            ) is True


class TestOneSaveOperationForCliAndAdmin:
    """The CLI reset invalidated dependents; the admin TUI did not — so the TUI could
    persist exactly the broken pair the CLI prevents."""

    @pytest.fixture(autouse=True)
    def _harnesses(self):
        register_builtin_harnesses()
        yield
        clear()

    def test_admin_save_goes_through_the_shared_operation(self):
        import inspect

        from agento.framework.admin import data

        source = inspect.getsource(data.set_config_value)
        assert "set_config_with_dependents" in source

    def test_cli_save_goes_through_the_shared_operation(self):
        from agento.framework.cli import config

        assert "set_config_with_dependents" in Path(config.__file__).read_text()

    def test_repair_uses_the_declared_default_provider_not_the_first_option(
        self, monkeypatch,
    ):
        """`allowed[0]` is declaration order — incidental. The harness's own
        `default_provider` is the intended answer."""
        from agento.framework.config_dependents import _preferred_value

        # codex declares exactly one provider, so make the ordering meaningful by
        # asserting the DEFAULT is chosen even when it is not first.
        assert _preferred_value(
            "agent_view/harness", "claude", ["openai", "anthropic"],
        ) == "anthropic"

    def test_falls_back_to_the_first_option_when_no_default_matches(self):
        from agento.framework.config_dependents import _preferred_value

        assert _preferred_value(
            "agent_view/harness", "claude", ["something_else"],
        ) == "something_else"

    def test_non_harness_parents_use_the_first_option(self):
        from agento.framework.config_dependents import _preferred_value

        assert _preferred_value("some/other", "x", ["a", "b"]) == "a"


class TestDevSandboxImageIsRendered:
    """The dev compose file builds the in-package Dockerfile directly, so that file must be
    a rendered artifact of the same template the deployment path uses — otherwise adding a
    harness still means editing framework Docker sources."""

    def test_template_carries_no_agent_specific_path(self):
        from agento.framework.cli._templates import get_template

        template = get_template("sandbox.Dockerfile")
        for hardcoded in (
            "@anthropic-ai/claude-code", "@openai/codex",
            "/usr/local/bin/claude", "/usr/local/bin/codex",
        ):
            assert hardcoded not in template, f"template hardcodes {hardcoded}"

    def test_self_update_grant_is_rendered_per_package(self):
        from agento.framework.cli._provisioning import (
            enumerate_sandbox_packages,
            render_sandbox_dockerfile,
        )
        from agento.framework.cli._templates import get_template

        packages = enumerate_sandbox_packages()
        rendered = render_sandbox_dockerfile(get_template("sandbox.Dockerfile"), packages)

        for pkg in packages:
            assert f"/usr/local/lib/node_modules/{pkg.package}" in rendered
            assert f"/usr/local/bin/{pkg.binary}" in rendered

    def test_a_third_harness_gets_its_own_grant_with_no_framework_edit(self):
        from agento.framework.cli._provisioning import render_sandbox_dockerfile
        from agento.framework.cli._templates import get_template
        from agento.framework.harness import SandboxPackage

        pkg = SandboxPackage.from_declaration({
            "manager": "npm", "package": "@example/hermes-cli", "binary": "hermes",
            "version_env_key": "HERMES_VERSION", "default_range": "1.0.0",
        }, "hermes")

        rendered = render_sandbox_dockerfile(get_template("sandbox.Dockerfile"), [pkg])

        assert "ARG HERMES_VERSION=1.0.0" in rendered
        assert '"@example/hermes-cli@${HERMES_VERSION}"' in rendered
        assert "/usr/local/lib/node_modules/@example/hermes-cli" in rendered
        assert "/usr/local/bin/hermes" in rendered

    def test_zero_packages_leaves_no_markers_behind(self):
        from agento.framework.cli._provisioning import render_sandbox_dockerfile
        from agento.framework.cli._templates import get_template

        rendered = render_sandbox_dockerfile(get_template("sandbox.Dockerfile"), [])

        assert "{{" not in rendered
