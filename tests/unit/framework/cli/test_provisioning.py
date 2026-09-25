"""Tests for _provisioning helpers used by `agento install` / `agento upgrade`."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.cli._provisioning import (
    SandboxPackage,
    build_base_images,
    bump_agento_version,
    detect_python_version,
    ensure_storage_dirs,
    enumerate_enabled_extensions,
    enumerate_sandbox_packages,
    localize_lockfile_for_container,
    materialize_docker_context,
    parse_semver_floor,
    regenerate_compose,
    render_compose,
    write_project_pyproject,
)


def _service_block(content: str, name: str) -> str:
    """The lines of one compose service, up to the next top-level service key.

    pyyaml is not a project dependency and adding one for a handful of
    assertions is not worth it; the template is generated from a fixed
    indentation, so slicing on it is exact.
    """
    lines = content.splitlines()
    start = lines.index(f"  {name}:")
    out: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() and line.startswith("  ") and not line.startswith("   "):
            break
        out.append(line)
    return "\n".join(out)


class TestWriteProjectPyproject:
    def test_writes_pinned_dependency(self, tmp_path: Path):
        write_project_pyproject(tmp_path, "myproj", "0.8.0")
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'name = "myproj"' in text
        assert 'agento-core==0.8.0' in text
        assert 'requires-python = ">=3.12"' in text


class TestBumpAgentoVersion:
    def test_replaces_version_pin(self, tmp_path: Path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[project]\n'
            'name = "x"\n'
            'dependencies = ["agento-core==0.7.7"]\n'
        )
        bump_agento_version(pp, "0.8.0")
        assert "agento-core==0.8.0" in pp.read_text()
        assert "0.7.7" not in pp.read_text()

    def test_handles_whitespace_around_eq(self, tmp_path: Path):
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[project]\n'
            'dependencies = ["agento-core == 0.7.0"]\n'
        )
        bump_agento_version(pp, "0.9.0")
        assert "agento-core==0.9.0" in pp.read_text()


class TestDetectPythonVersion:
    def test_reads_pyvenv_cfg(self, tmp_path: Path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text(
            "home = /usr/bin\n"
            "version_info = 3.12.7.final.0\n"
        )
        assert detect_python_version(venv) == "3.12"

    def test_reads_version_key(self, tmp_path: Path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("version = 3.13.1\n")
        assert detect_python_version(venv) == "3.13"

    def test_falls_back_to_default_when_missing(self, tmp_path: Path):
        # Non-existent venv → fallback "3.12"
        assert detect_python_version(tmp_path / ".venv") == "3.12"

    def test_falls_back_when_unparseable(self, tmp_path: Path):
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("version_info = garbage\n")
        assert detect_python_version(venv) == "3.12"


class TestEnumerateEnabledExtensions:
    def _make_project(self, tmp_path: Path) -> Path:
        (tmp_path / "app" / "etc").mkdir(parents=True)
        (tmp_path / "app" / "code").mkdir(parents=True)
        (tmp_path / ".venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
        return tmp_path

    def test_lists_only_enabled_pypi_extensions(self, tmp_path: Path):
        proj = self._make_project(tmp_path)
        site = proj / ".venv" / "lib" / "python3.12" / "site-packages"
        for name in ("ext_a", "ext_b", "ext_c"):
            (site / name).mkdir()
            (site / name / "__init__.py").write_text("")

        (proj / "app" / "etc" / "modules.json").write_text(
            json.dumps({"ext_a": True, "ext_b": False, "ext_c": True})
        )

        result = enumerate_enabled_extensions(proj)
        assert result == ["ext_a", "ext_c"]

    def test_excludes_local_modules(self, tmp_path: Path):
        proj = self._make_project(tmp_path)
        site = proj / ".venv" / "lib" / "python3.12" / "site-packages"

        # Same-name local module shadows the PyPI one — local wins.
        (site / "k3_jira").mkdir()
        (site / "k3_jira" / "__init__.py").write_text("")
        (proj / "app" / "code" / "k3_jira").mkdir()
        (proj / "app" / "code" / "k3_jira" / "module.json").write_text("{}")

        (proj / "app" / "etc" / "modules.json").write_text(
            json.dumps({"k3_jira": True})
        )

        # Local takes precedence — not included in PyPI mounts.
        assert enumerate_enabled_extensions(proj) == []

    def test_excludes_disabled_and_missing(self, tmp_path: Path):
        proj = self._make_project(tmp_path)
        (proj / "app" / "etc" / "modules.json").write_text(
            json.dumps({"ghost": True, "off": False})
        )
        # Neither package is installed in venv; both are missing.
        assert enumerate_enabled_extensions(proj) == []


class TestRenderCompose:
    TEMPLATE = (
        "services:\n"
        "  cron:\n"
        "    volumes:\n"
        "      - ../.venv/lib/python{{ python_version }}/site-packages/agento:/opt/agento-src/agento:ro\n"
        "      # {{ extension_mounts_cron }}\n"
        "    environment:\n"
        "      - PY={{ python_version }}\n"
    )

    def test_substitutes_python_version(self):
        out = render_compose(
            self.TEMPLATE, python_version="3.13", extensions=[], sandbox_packages=[],
        )
        assert "python3.13" in out
        assert "PY=3.13" in out

    def test_no_extensions_removes_placeholder_line(self):
        out = render_compose(
            self.TEMPLATE, python_version="3.12", extensions=[], sandbox_packages=[],
        )
        assert "extension_mounts_cron" not in out
        # Placeholder line is gone — no orphan comment markers in output.
        assert "# " not in out.split("environment:")[0].splitlines()[-1]

    def test_extensions_are_inserted(self):
        out = render_compose(
            self.TEMPLATE,
            python_version="3.12",
            extensions=["ext_a", "ext_b"],
            sandbox_packages=[],
        )
        assert "/site-packages/ext_a:/opt/agento-src/ext_a:ro" in out
        assert "/site-packages/ext_b:/opt/agento-src/ext_b:ro" in out
        # Mounts come before `environment:` block.
        assert out.index("ext_a") < out.index("environment:")


class TestMaterializeDockerContext:
    def _seed_project(self, tmp_path: Path) -> Path:
        # Minimal project: pyproject.toml + uv.lock so cron context is complete.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["agento-core==0.8.0"]\n'
        )
        (tmp_path / "uv.lock").write_text("# lock\n")
        return tmp_path

    def test_writes_dockerfiles_and_stamp(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        materialize_docker_context(proj, force=True)
        target = proj / ".agento" / "docker"
        assert (target / "sandbox" / "Dockerfile").is_file()
        assert (target / "cron" / "Dockerfile").is_file()
        assert (target / "toolbox" / "Dockerfile").is_file()
        assert (target / "version").is_file()

    def test_copies_the_proxy_config(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        materialize_docker_context(proj, force=True)
        proxy = proj / ".agento" / "docker" / "proxy"
        assert (proxy / "Caddyfile").is_file()
        assert (proxy / "entrypoint.sh").is_file()

    def test_copies_project_pyproject_into_cron_context(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        materialize_docker_context(proj, force=True)
        copied = (proj / ".agento" / "docker" / "cron" / "pyproject.toml").read_text()
        assert "agento-core==0.8.0" in copied
        assert (proj / ".agento" / "docker" / "cron" / "uv.lock").read_text() == "# lock\n"

    def test_idempotent_when_stamp_matches(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        materialize_docker_context(proj, force=True)
        # Mutate the stamped tree — a second call without force MUST NOT touch it.
        marker = proj / ".agento" / "docker" / "cron" / "Dockerfile"
        marker.write_text("HELLO_MARKER\n")
        materialize_docker_context(proj, force=False)
        assert marker.read_text() == "HELLO_MARKER\n"

    def test_force_reinitializes(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        materialize_docker_context(proj, force=True)
        marker = proj / ".agento" / "docker" / "cron" / "Dockerfile"
        marker.write_text("HELLO_MARKER\n")
        materialize_docker_context(proj, force=True)
        # force=True wipes and recopies — original Dockerfile content is restored.
        assert "HELLO_MARKER" not in marker.read_text()


class TestLocalizeLockfileForContainer:
    def _seed(self, tmp_path: Path) -> tuple[Path, Path]:
        """Build a project + cron context simulating a local-wheel install."""
        project = tmp_path / "proj"
        project.mkdir()

        # Local wheel registry that lives outside the project tree.
        dist = tmp_path / "host" / "dist"
        dist.mkdir(parents=True)
        (dist / "agento_core-0.8.0-py3-none-any.whl").write_bytes(b"WHEEL_BYTES")
        (dist / "agento_core-0.8.0.tar.gz").write_bytes(b"SDIST_BYTES")

        # uv records the registry as a path relative to the project's uv.lock.
        rel = "../host/dist"
        lock = (
            'version = 1\n'
            'revision = 2\n'
            'requires-python = ">=3.12"\n'
            '\n'
            '[[package]]\n'
            'name = "agento-core"\n'
            'version = "0.8.0"\n'
            f'source = {{ registry = "{rel}" }}\n'
            'sdist = { path = "agento_core-0.8.0.tar.gz" }\n'
            'wheels = [\n'
            '    { path = "agento_core-0.8.0-py3-none-any.whl" },\n'
            ']\n'
            '\n'
            '[[package]]\n'
            'name = "anyio"\n'
            'version = "4.13.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
        )
        (project / "uv.lock").write_text(lock)

        cron_ctx = tmp_path / "cron"
        cron_ctx.mkdir()
        (cron_ctx / "uv.lock").write_text(lock)
        return project, cron_ctx

    def test_creates_local_dist_dir_with_gitkeep(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        cron_ctx = tmp_path / "cron"
        cron_ctx.mkdir()
        # No uv.lock at all — still creates the directory so Dockerfile COPY works.
        localize_lockfile_for_container(project, cron_ctx)
        assert (cron_ctx / "_local_dist").is_dir()
        assert (cron_ctx / "_local_dist" / ".gitkeep").is_file()

    def test_inlines_local_wheels_and_rewrites_registry(self, tmp_path: Path):
        project, cron_ctx = self._seed(tmp_path)
        localize_lockfile_for_container(project, cron_ctx)

        # Wheel + sdist copied into _local_dist/ (next to the lockfile in the container).
        local_dist = cron_ctx / "_local_dist"
        assert (local_dist / "agento_core-0.8.0-py3-none-any.whl").read_bytes() == b"WHEEL_BYTES"
        assert (local_dist / "agento_core-0.8.0.tar.gz").read_bytes() == b"SDIST_BYTES"

        # Lockfile registry path is now relative to the in-container lockfile location.
        lock_text = (cron_ctx / "uv.lock").read_text()
        assert 'source = { registry = "_local_dist" }' in lock_text
        assert "../host/dist" not in lock_text
        # PyPI registry entries are untouched.
        assert 'source = { registry = "https://pypi.org/simple" }' in lock_text

    def test_pypi_only_lockfile_is_unchanged(self, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        cron_ctx = tmp_path / "cron"
        cron_ctx.mkdir()
        lock = (
            '[[package]]\n'
            'name = "anyio"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
        )
        (project / "uv.lock").write_text(lock)
        (cron_ctx / "uv.lock").write_text(lock)

        localize_lockfile_for_container(project, cron_ctx)
        assert (cron_ctx / "uv.lock").read_text() == lock
        # _local_dist still gets created (Dockerfile COPY needs the dir).
        assert (cron_ctx / "_local_dist").is_dir()


class TestRegenerateCompose:
    def _seed_project(self, tmp_path: Path) -> Path:
        (tmp_path / "docker").mkdir()
        (tmp_path / "app" / "etc").mkdir(parents=True)
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("version_info = 3.12.7\n")
        return tmp_path

    def test_writes_compose_file_with_python_version(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        regenerate_compose(proj)
        content = (proj / "docker" / "docker-compose.yml").read_text()
        assert "python3.12" in content
        # No GHCR images — local build only.
        assert "ghcr.io" not in content
        assert "build:" in content

    def test_inserts_pypi_extension_mounts(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        site = proj / ".venv" / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        (site / "agento_jira_ext").mkdir()
        (site / "agento_jira_ext" / "__init__.py").write_text("")
        (proj / "app" / "etc" / "modules.json").write_text(
            json.dumps({"agento_jira_ext": True})
        )

        regenerate_compose(proj)
        content = (proj / "docker" / "docker-compose.yml").read_text()
        assert "agento_jira_ext:/opt/agento-src/agento_jira_ext:ro" in content

    def test_no_extensions_means_no_orphan_placeholder(self, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        regenerate_compose(proj)
        content = (proj / "docker" / "docker-compose.yml").read_text()
        # Neither raw placeholder nor stray comment marker for it.
        assert "extension_mounts_sandbox" not in content
        assert "extension_mounts_cron" not in content

    def test_includes_sandbox_package_pins_from_core_modules(self, tmp_path: Path):
        # Core agent modules ship sandbox_packages declarations — regenerate_compose
        # must inject those into the sandbox build-args block (or the sandbox image
        # would have no CLI version pinning at all).
        proj = self._seed_project(tmp_path)
        regenerate_compose(proj)
        content = (proj / "docker" / "docker-compose.yml").read_text()
        # The marker line must not survive rendering.
        assert "sandbox_package_args" not in content
        # Claude + codex ship with the framework; both should appear under sandbox.args.
        assert "CLAUDE_CODE_VERSION: ${CLAUDE_CODE_VERSION:-" in content
        assert "CODEX_VERSION: ${CODEX_VERSION:-" in content


class TestBuildBaseImages:
    """build_base_images guarantees agento-<service>:<version> tags exist before
    docker compose build runs — so an override that re-bases on the managed
    tag (e.g. FROM agento-toolbox:${AGENTO_VERSION}) doesn't fail to pull."""

    def _seed_project(self, tmp_path: Path) -> Path:
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / ".env").write_text(
            "AGENTO_VERSION=0.9.4\nHOST_UID=1500\nHOST_GID=2500\n"
            "CLAUDE_CODE_VERSION=~2.1.150\nCODEX_VERSION=~0.130.0\n"
        )
        ctx = tmp_path / ".agento" / "docker"
        (ctx / "sandbox").mkdir(parents=True)
        (ctx / "toolbox").mkdir(parents=True)
        (ctx / "cron").mkdir(parents=True)
        return tmp_path

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_builds_three_services_in_order(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.4")

        invocations = [list(call.args[0]) for call in mock_run.call_args_list]
        # Exactly three docker build invocations, sandbox → toolbox → cron.
        assert len(invocations) == 3
        tags = [inv[inv.index("-t") + 1] for inv in invocations]
        assert tags == [
            "agento-sandbox:0.9.4",
            "agento-toolbox:0.9.4",
            "agento-cron:0.9.4",
        ]
        # Each invocation starts with `docker build`.
        for inv in invocations:
            assert inv[:2] == ["docker", "build"]

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_passes_host_uid_gid_to_sandbox_and_toolbox(
        self, mock_run, tmp_path: Path
    ):
        proj = self._seed_project(tmp_path)
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.4")

        invocations = [list(call.args[0]) for call in mock_run.call_args_list]
        for inv in invocations[:2]:  # sandbox, toolbox
            assert "--build-arg" in inv
            assert "HOST_UID=1500" in inv
            assert "HOST_GID=2500" in inv

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_passes_sandbox_image_arg_to_cron(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.4")

        cron_inv = list(mock_run.call_args_list[2].args[0])
        assert "SANDBOX_IMAGE=agento-sandbox:0.9.4" in cron_inv

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_uses_agento_docker_context_paths(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.4")

        invocations = [list(call.args[0]) for call in mock_run.call_args_list]
        contexts = [inv[-1] for inv in invocations]
        assert contexts == [
            str(proj / ".agento" / "docker" / "sandbox"),
            str(proj / ".agento" / "docker" / "toolbox"),
            str(proj / ".agento" / "docker" / "cron"),
        ]

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_exits_on_build_failure(self, mock_run, tmp_path: Path):
        proj = self._seed_project(tmp_path)
        mock_run.return_value = type("R", (), {"returncode": 1})()

        with pytest.raises(SystemExit) as exc:
            build_base_images(proj, "0.9.4")
        assert exc.value.code == 1
        # Should fail fast on first failure — exactly one call.
        assert mock_run.call_count == 1

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_falls_back_to_default_host_ids_when_env_missing(
        self, mock_run, tmp_path: Path
    ):
        # docker/.env without HOST_UID/HOST_GID — should default to 1000/1000.
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / ".env").write_text("AGENTO_VERSION=0.9.4\n")
        ctx = tmp_path / ".agento" / "docker"
        for s in ("sandbox", "toolbox", "cron"):
            (ctx / s).mkdir(parents=True)
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(tmp_path, "0.9.4")

        sandbox_inv = list(mock_run.call_args_list[0].args[0])
        assert "HOST_UID=1000" in sandbox_inv
        assert "HOST_GID=1000" in sandbox_inv


class TestBuildBaseImagesTemplateDriftGuard:
    """Catches future docker-compose.yml template changes that add build args
    without updating build_base_images.

    Renders the template against the current core registry, then parses the
    output for build-arg keys per service. Both sides (template + helper) read
    from the same di.json declarations, so the assertion is that any agent
    module shipping a sandbox_packages entry shows up in BOTH the rendered
    compose and the loop in build_base_images."""

    def test_helper_matches_rendered_template_build_args(self):
        import re

        from agento.framework.cli._templates import get_template

        template = get_template("docker-compose.yml")
        core_packages = enumerate_sandbox_packages()
        rendered = render_compose(
            template,
            python_version="3.12",
            extensions=[],
            sandbox_packages=core_packages,
        )

        lines = rendered.splitlines()
        service_re = re.compile(r"^  (\w+):\s*$")
        arg_re = re.compile(r"^        ([A-Z_]+):\s*")
        actual: dict[str, set[str]] = {}
        current_service: str | None = None
        in_args = False
        for line in lines:
            m = service_re.match(line)
            if m:
                current_service = m.group(1)
                in_args = False
                actual.setdefault(current_service, set())
                continue
            if current_service is None:
                continue
            if line.startswith("      args:"):
                in_args = True
                continue
            if in_args:
                arg_m = arg_re.match(line)
                if arg_m:
                    actual[current_service].add(arg_m.group(1))
                elif not line.startswith("        "):
                    in_args = False

        # Sandbox args = static (HOST_UID/HOST_GID) + one env key per registered
        # sandbox_package. Toolbox/cron are agent-agnostic.
        registry_keys = {pkg.version_env_key for pkg in core_packages}
        expected = {
            "sandbox": {"HOST_UID", "HOST_GID"} | registry_keys,
            "toolbox": {"HOST_UID", "HOST_GID"},
            "cron": {"SANDBOX_IMAGE"},
        }
        for service, args in expected.items():
            assert actual.get(service) == args, (
                f"rendered template out of sync with helper for {service}: "
                f"template={actual.get(service)}, expected={args}"
            )


class TestParseSemverFloor:
    def test_plain_semver(self):
        assert parse_semver_floor("2.1.142") == (2, 1, 142)

    def test_tilde_range(self):
        assert parse_semver_floor("~2.1.142") == (2, 1, 142)

    def test_caret_range(self):
        assert parse_semver_floor("^2.1.0") == (2, 1, 0)

    def test_version_output_string(self):
        # claude --version emits e.g. "2.1.126 (Claude Code)"
        assert parse_semver_floor("2.1.126 (Claude Code)") == (2, 1, 126)

    def test_codex_output_string(self):
        # codex --version emits e.g. "codex-cli 0.128.0"
        assert parse_semver_floor("codex-cli 0.128.0") == (0, 128, 0)

    def test_unparseable_returns_none(self):
        assert parse_semver_floor("latest") is None
        assert parse_semver_floor("") is None


class TestBuildBaseImagesCliPins:
    """build_base_images must propagate sandbox_packages pins from docker/.env
    to the sandbox image, falling back to each module's default_range when the
    .env doesn't set them. Pin propagation is driven by the di.json registry,
    not by hardcoded harness names."""

    def _seed_with_env(self, tmp_path: Path, env_text: str) -> Path:
        (tmp_path / "docker").mkdir()
        (tmp_path / "docker" / ".env").write_text(env_text)
        ctx = tmp_path / ".agento" / "docker"
        for s in ("sandbox", "toolbox", "cron"):
            (ctx / s).mkdir(parents=True)
        return tmp_path

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_propagates_env_pins_to_sandbox_build(
        self, mock_run, tmp_path: Path,
    ):
        proj = self._seed_with_env(
            tmp_path,
            "HOST_UID=1000\nHOST_GID=1000\n"
            "CLAUDE_CODE_VERSION=~2.1.150\nCODEX_VERSION=~0.130.0\n",
        )
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.6")

        sandbox_inv = list(mock_run.call_args_list[0].args[0])
        assert "CLAUDE_CODE_VERSION=~2.1.150" in sandbox_inv
        assert "CODEX_VERSION=~0.130.0" in sandbox_inv

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_falls_back_to_module_defaults_when_env_missing_pins(
        self, mock_run, tmp_path: Path,
    ):
        # .env exists but only carries non-CLI keys — should fall back to the
        # default_range declared in each agent module's di.json.
        proj = self._seed_with_env(tmp_path, "HOST_UID=1000\nHOST_GID=1000\n")
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.6")

        sandbox_inv = list(mock_run.call_args_list[0].args[0])
        for pkg in enumerate_sandbox_packages(proj):
            assert f"{pkg.version_env_key}={pkg.default_range}" in sandbox_inv

    @patch("agento.framework.cli._provisioning.subprocess.run")
    def test_does_not_pass_cli_pins_to_toolbox_or_cron(
        self, mock_run, tmp_path: Path,
    ):
        proj = self._seed_with_env(
            tmp_path,
            "CLAUDE_CODE_VERSION=~2.1.150\nCODEX_VERSION=~0.130.0\n",
        )
        mock_run.return_value = type("R", (), {"returncode": 0})()

        build_base_images(proj, "0.9.6")

        # Toolbox and cron Dockerfiles don't accept these ARGs; passing them
        # would be harmless but noisy. Keep the scope tight to sandbox.
        pin_keys = [pkg.version_env_key for pkg in enumerate_sandbox_packages(proj)]
        for idx in (1, 2):
            inv = list(mock_run.call_args_list[idx].args[0])
            for key in pin_keys:
                assert not any(a.startswith(f"{key}=") for a in inv)


class TestSandboxDockerfileIsRendered:
    """The dev sandbox Dockerfile must be a RENDERED artifact of the one template.

    Deployment renders ``cli/templates/sandbox.Dockerfile`` into ``.agento/docker/sandbox/``,
    but ``docker/docker-compose.dev.yml`` builds the in-package
    ``framework/docker/sandbox/Dockerfile`` directly. While that file hardcoded
    ``ARG CLAUDE_CODE_VERSION`` / ``ARG CODEX_VERSION`` and the ``npm install -g`` line, the
    two paths were separate sources of truth and adding a harness still meant editing
    framework Docker sources — the exact thing AGENTS.md rule #6 forbids. The previous
    guard here only compared ARG defaults, which could not catch a MISSING harness.
    """

    def test_dev_dockerfile_equals_a_fresh_render(self):
        from agento.framework.cli._provisioning import (
            dev_sandbox_dockerfile_path,
            render_sandbox_dockerfile,
        )
        from agento.framework.cli._templates import get_template

        expected = render_sandbox_dockerfile(
            get_template("sandbox.Dockerfile"), enumerate_sandbox_packages(),
        )
        actual = dev_sandbox_dockerfile_path().read_text()

        assert actual == expected, (
            "framework/docker/sandbox/Dockerfile has drifted from the template. "
            "Regenerate it: python -c 'from agento.framework.cli._provisioning import "
            "render_dev_sandbox_dockerfile as r; r()'"
        )

    def test_no_agent_cli_is_hardcoded_in_the_template(self):
        """The template must carry markers, never a package name."""
        from agento.framework.cli._templates import get_template

        template = get_template("sandbox.Dockerfile")
        assert "{{ sandbox_package_args }}" in template
        assert "{{ sandbox_package_install }}" in template
        for name in ("@anthropic-ai/claude-code", "@openai/codex"):
            assert name not in template, f"template hardcodes {name}"

    def test_every_declared_package_reaches_the_dev_image(self):
        from agento.framework.cli._provisioning import dev_sandbox_dockerfile_path

        text = dev_sandbox_dockerfile_path().read_text()
        for pkg in enumerate_sandbox_packages():
            assert f"ARG {pkg.version_env_key}={pkg.default_range}" in text
            assert f'"{pkg.package}@${{{pkg.version_env_key}}}"' in text

    def test_dev_compose_defaults_match_di_json_defaults(self):
        """Dev compose passes the pins as build args; they must match the declarations.
        Skips cleanly outside the repo (e.g. against an installed wheel)."""
        from pathlib import Path

        dev_compose = (
            Path(__file__).resolve().parents[4] / "docker" / "docker-compose.dev.yml"
        )
        if not dev_compose.is_file():
            return
        dev_text = dev_compose.read_text()
        for pkg in enumerate_sandbox_packages():
            expected = (
                f"{pkg.version_env_key}: "
                f"${{{pkg.version_env_key}:-{pkg.default_range}}}"
            )
            assert expected in dev_text, (
                f"docker/docker-compose.dev.yml missing or stale build arg "
                f"for {pkg.version_env_key} (expected '{expected}')"
            )


class TestEnumerateSandboxPackages:
    """Registry enumeration is the single source of truth for which agents the
    sandbox image needs to install. Tests cover: core-only enumeration, the
    user app/code/ overlay, modules.json disable, duplicate version_env_key
    detection, and the empty-list path."""

    def test_core_modules_alone_yield_claude_and_codex(self):
        # Without a project root, only framework-shipped core modules contribute.
        # Claude and codex both declare sandbox_packages.
        pkgs = enumerate_sandbox_packages()
        keys = {p.version_env_key for p in pkgs}
        assert "CLAUDE_CODE_VERSION" in keys
        assert "CODEX_VERSION" in keys
        # Each entry has the expected dataclass shape.
        for p in pkgs:
            assert isinstance(p, SandboxPackage)
            assert p.manager == "npm"
            assert p.binary
            assert p.package

    def _seed_project_with_local_module(
        self, tmp_path: Path, *, name: str, env_key: str, default: str,
    ) -> Path:
        mod = tmp_path / "app" / "code" / name
        mod.mkdir(parents=True)
        (mod / "module.json").write_text(json.dumps({"name": name, "version": "0.1.0"}))
        (mod / "di.json").write_text(json.dumps({
            "sandbox_packages": [{
                "provider": name,
                "manager": "npm",
                "package": f"@example/{name}-cli",
                "binary": name,
                "version_env_key": env_key,
                "default_range": default,
            }]
        }))
        return tmp_path

    def test_local_module_overlays_on_core(self, tmp_path: Path):
        # A local module under app/code/ adds its sandbox_packages to whatever
        # the core modules declare — neither shadows the other when env keys
        # are distinct.
        proj = self._seed_project_with_local_module(
            tmp_path, name="hermes", env_key="HERMES_VERSION", default="~1.0.0",
        )
        pkgs = enumerate_sandbox_packages(proj)
        keys = {p.version_env_key for p in pkgs}
        assert "HERMES_VERSION" in keys
        # Core modules still contribute.
        assert "CLAUDE_CODE_VERSION" in keys

    def test_disabled_local_module_is_excluded(self, tmp_path: Path):
        proj = self._seed_project_with_local_module(
            tmp_path, name="hermes", env_key="HERMES_VERSION", default="~1.0.0",
        )
        (proj / "app" / "etc").mkdir(parents=True, exist_ok=True)
        (proj / "app" / "etc" / "modules.json").write_text(
            json.dumps({"hermes": False, "claude": True, "codex": True})
        )

        pkgs = enumerate_sandbox_packages(proj)
        keys = {p.version_env_key for p in pkgs}
        assert "HERMES_VERSION" not in keys
        # Explicitly-enabled core modules still appear.
        assert "CLAUDE_CODE_VERSION" in keys

    def test_modules_default_enabled_when_absent_from_status(self, tmp_path: Path):
        # modules.json doesn't list hermes — default-enabled stance kicks in.
        proj = self._seed_project_with_local_module(
            tmp_path, name="hermes", env_key="HERMES_VERSION", default="~1.0.0",
        )
        (proj / "app" / "etc").mkdir(parents=True, exist_ok=True)
        (proj / "app" / "etc" / "modules.json").write_text(
            json.dumps({"claude": True})  # hermes absent → defaults to enabled
        )

        pkgs = enumerate_sandbox_packages(proj)
        keys = {p.version_env_key for p in pkgs}
        assert "HERMES_VERSION" in keys

    def test_duplicate_env_key_across_modules_raises(self, tmp_path: Path):
        # Two distinct modules declaring the same version_env_key would
        # silently overwrite each other's pin in docker/.env — make this
        # a hard error so a copy-paste collision is caught early.
        proj = self._seed_project_with_local_module(
            tmp_path, name="dupe1", env_key="CLAUDE_CODE_VERSION", default="~9.9.9",
        )
        # CLAUDE_CODE_VERSION is already claimed by the core claude module.
        with pytest.raises(RuntimeError, match="duplicate sandbox_packages"):
            enumerate_sandbox_packages(proj)

    def test_malformed_entry_raises(self, tmp_path: Path):
        # Missing required fields in a declaration must not be silently dropped.
        mod = tmp_path / "app" / "code" / "broken"
        mod.mkdir(parents=True)
        (mod / "module.json").write_text(json.dumps({"name": "broken"}))
        (mod / "di.json").write_text(json.dumps({
            "sandbox_packages": [{"provider": "broken"}]  # missing everything else
        }))

        with pytest.raises(RuntimeError, match="Malformed sandbox_packages"):
            enumerate_sandbox_packages(tmp_path)

    def test_module_without_sandbox_packages_is_skipped(self, tmp_path: Path):
        mod = tmp_path / "app" / "code" / "no_sandbox"
        mod.mkdir(parents=True)
        (mod / "module.json").write_text(json.dumps({"name": "no_sandbox"}))
        (mod / "di.json").write_text(json.dumps({"runtimes": []}))

        # Should not raise; only core packages come back.
        pkgs = enumerate_sandbox_packages(tmp_path)
        assert all(p.harness != "no_sandbox" for p in pkgs)


class TestRenderComposeWithSandboxPackages:
    """The sandbox build-args block is rendered from the registry. Test the
    renderer in isolation: 0 packages removes the marker line; N packages
    expands into N indented `KEY: ${KEY:-default}` lines under `args:`."""

    TEMPLATE = (
        "services:\n"
        "  sandbox:\n"
        "    build:\n"
        "      context: ./sandbox\n"
        "      args:\n"
        "        HOST_UID: ${HOST_UID:-1000}\n"
        "        # {{ sandbox_package_args }}\n"
        "    image: agento-sandbox:latest\n"
    )

    def test_zero_packages_removes_marker_line(self):
        out = render_compose(
            self.TEMPLATE, python_version="3.12", extensions=[], sandbox_packages=[],
        )
        assert "sandbox_package_args" not in out
        # HOST_UID stays on its own line; no stray blank line where the marker was.
        assert "HOST_UID: ${HOST_UID:-1000}\n    image:" in out

    def test_single_package_renders_one_line(self):
        pkg = SandboxPackage(
            harness="claude", manager="npm", package="@anthropic-ai/claude-code",
            binary="claude", version_env_key="CLAUDE_CODE_VERSION", default_range="~2.1.142",
        )
        out = render_compose(
            self.TEMPLATE, python_version="3.12", extensions=[], sandbox_packages=[pkg],
        )
        assert "CLAUDE_CODE_VERSION: ${CLAUDE_CODE_VERSION:-~2.1.142}" in out
        assert "sandbox_package_args" not in out

    def test_multiple_packages_render_in_order(self):
        pkgs = [
            SandboxPackage(
                harness="claude", manager="npm", package="@anthropic-ai/claude-code",
                binary="claude", version_env_key="CLAUDE_CODE_VERSION",
                default_range="~2.1.142",
            ),
            SandboxPackage(
                harness="codex", manager="npm", package="@openai/codex",
                binary="codex", version_env_key="CODEX_VERSION",
                default_range="~0.128.0",
            ),
            SandboxPackage(
                harness="hermes", manager="npm", package="@example/hermes",
                binary="hermes", version_env_key="HERMES_VERSION",
                default_range="~1.0.0",
            ),
        ]
        out = render_compose(
            self.TEMPLATE, python_version="3.12", extensions=[], sandbox_packages=pkgs,
        )
        # Each pin renders once, in declared order.
        positions = [
            out.index("CLAUDE_CODE_VERSION:"),
            out.index("CODEX_VERSION:"),
            out.index("HERMES_VERSION:"),
        ]
        assert positions == sorted(positions)


class TestNewAgentRegistersWithoutFrameworkEdit:
    """Pins the registry contract: dropping a new agent module under app/code/
    with a sandbox_packages entry must make it discoverable to all CLI
    commands without editing any framework file.

    (The Dockerfile templating follow-up will extend this to actual npm
    installation; this PR keeps the Dockerfile static so a new agent's CLI
    won't be installed in the image yet — but the registry contract is in
    place, which is the structural fix.)"""

    def test_new_agent_appears_in_enumeration_and_rendered_compose(
        self, tmp_path: Path,
    ):
        from agento.framework.cli._templates import get_template

        mod = tmp_path / "app" / "code" / "hermes"
        mod.mkdir(parents=True)
        (mod / "module.json").write_text(json.dumps({"name": "hermes", "version": "0.1.0"}))
        (mod / "di.json").write_text(json.dumps({
            "sandbox_packages": [{
                "provider": "hermes",
                "manager": "npm",
                "package": "@example/hermes-cli",
                "binary": "hermes",
                "version_env_key": "HERMES_VERSION",
                "default_range": "~1.0.0",
            }]
        }))

        # 1. Enumeration includes the new agent.
        pkgs = enumerate_sandbox_packages(tmp_path)
        hermes = next((p for p in pkgs if p.version_env_key == "HERMES_VERSION"), None)
        assert hermes is not None
        assert hermes.binary == "hermes"
        assert hermes.default_range == "~1.0.0"

        # 2. Rendered compose carries the new build arg.
        template = get_template("docker-compose.yml")
        rendered = render_compose(
            template, python_version="3.12", extensions=[], sandbox_packages=pkgs,
        )
        assert "HERMES_VERSION: ${HERMES_VERSION:-~1.0.0}" in rendered


class TestEnsureStorageDirs:
    def test_creates_the_store_and_published_roots(self, tmp_path: Path):
        ensure_storage_dirs(tmp_path)

        assert (tmp_path / "storage" / "versioned-artifacts" / "store").is_dir()
        assert (tmp_path / "storage" / "versioned-artifacts" / "published").is_dir()

    def test_is_idempotent(self, tmp_path: Path):
        ensure_storage_dirs(tmp_path)
        marker = tmp_path / "storage" / "versioned-artifacts" / "store" / "keep"
        marker.mkdir()

        ensure_storage_dirs(tmp_path)

        assert marker.is_dir()


class TestToolboxStorageBind:
    def test_toolbox_mounts_the_store_root(self, tmp_path: Path):
        proj = tmp_path
        (proj / "docker").mkdir()
        (proj / "app" / "etc").mkdir(parents=True)
        venv = proj / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("version_info = 3.12.7\n")

        regenerate_compose(proj)

        # Exact, so a botched rename that leaves a second store bind behind fails
        # here rather than silently mounting two roots. Scoped to the toolbox service:
        # the artifacts service has its own, deliberately narrower, /srv/ bind.
        content = (proj / "docker" / "docker-compose.yml").read_text()
        block = _service_block(content, "toolbox")
        binds = [ln.strip() for ln in block.splitlines() if "/srv/" in ln]
        assert binds == ["- ../storage/versioned-artifacts:/srv/versioned-artifacts"]


class TestArtifactsService:
    """The serving container, rendered from the shipped template.

    Its isolation is the whole security story: no `networks:` means no agent can
    reach it, no `env_file:`/`environment:` means it holds no secret, and the
    published-tree mount means it cannot see the store even if it wanted to.
    """

    def _block(self) -> str:
        from agento.framework.cli._templates import get_template

        rendered = render_compose(
            get_template("docker-compose.yml"),
            python_version="3.12",
            extensions=[],
            sandbox_packages=[],
        )
        return _service_block(rendered, "artifacts")

    def test_joins_no_network_and_reads_no_secret(self):
        block = self._block()
        # One `networks:` line added for consistency would put every artifact on
        # agento-net, readable by every agent in every agent_view over plain HTTP.
        assert "networks:" not in block
        assert "env_file:" not in block
        assert "environment:" not in block

    def test_has_no_build_block_and_reuses_the_toolbox_image(self):
        # `build_base_images` has a hardcoded three-entry specs list and would
        # never build a tag of its own.
        block = self._block()
        assert "build:" not in block
        assert "image: agento-toolbox:" in block

    def test_carries_its_own_healthcheck(self):
        # The baked HEALTHCHECK probes 3001 and reports this container unhealthy
        # while it serves fine.
        block = self._block()
        assert "healthcheck:" in block
        assert "127.0.0.1:8080/" in block

    def test_mounts_the_published_tree_exactly_and_read_only(self):
        block = self._block()
        mounts = [ln.strip()[2:] for ln in block.splitlines() if ln.strip().startswith("- ../")]
        store = [m for m in mounts if "versioned-artifacts" in m]
        assert len(store) == 1
        source, _, rest = store[0].partition(":")
        assert source.endswith("/published")
        assert rest.endswith(":ro")
        assert all(m.endswith(":ro") for m in mounts)

    def test_is_hardened_and_restarts(self):
        block = self._block()
        assert "no-new-privileges:true" in block
        assert "restart: unless-stopped" in block

    def test_the_dev_compose_carries_the_same_service(self):
        """The provisioning path renders only the template, so the dev file is the
        copy that silently drifts. Skips cleanly outside the repo."""
        dev_compose = (
            Path(__file__).resolve().parents[4] / "docker" / "docker-compose.dev.yml"
        )
        if not dev_compose.is_file():
            return

        def norm(block: str) -> str:
            return (
                block.replace(
                    "../.venv/lib/python3.12/site-packages/agento/modules", "MODULES"
                )
                .replace("../src/agento/modules", "MODULES")
                .replace("agento-toolbox:${AGENTO_VERSION:-latest}", "agento-toolbox:TAG")
                .replace("agento-toolbox:latest", "agento-toolbox:TAG")
            )

        dev = _service_block(dev_compose.read_text(), "artifacts")
        assert norm(dev) == norm(self._block()), (
            "docker/docker-compose.dev.yml artifacts service has drifted from "
            "templates/docker-compose.yml (only the modules mount and the image "
            "tag may differ)"
        )


class TestDocumentedContainerInventory:
    """A service is added to compose and the prose that counts containers stays behind —
    that is the defect class, and it has now cost two review rounds. Compare the
    architecture doc's table against the rendered template instead of grepping for a
    number, so the next new service fails here rather than in a review."""

    def test_the_services_table_lists_exactly_the_rendered_services(self):
        import re

        from agento.framework.cli._templates import get_template

        doc = (
            Path(__file__).resolve().parents[4]
            / "docs" / "architecture" / "containers.md"
        )
        if not doc.is_file():
            return

        rendered = render_compose(
            get_template("docker-compose.yml"),
            python_version="3.12",
            extensions=[],
            sandbox_packages=[],
        )
        # Only the block under `services:` — a 2-space key elsewhere is a network name.
        body = rendered.split("\nservices:\n", 1)[1]
        body = re.split(r"\n(?=\S)", body, maxsplit=1)[0]
        services = set(re.findall(r"^  ([a-z][a-z0-9_-]*):\s*$", body, re.M))
        documented = set(re.findall(r"^\| \*\*([a-z][a-z0-9_-]*)\*\* \|", doc.read_text(), re.M))

        assert services, "no services parsed out of the rendered template"
        assert documented == services, (
            f"docs/architecture/containers.md lists {sorted(documented)}, "
            f"compose renders {sorted(services)}"
        )


def _items(block: str, key: str) -> list[str]:
    """The `- item` lines directly under a 4-space `key:` of one service block."""
    lines = block.splitlines()
    try:
        start = lines.index(f"    {key}:")
    except ValueError:
        return []
    out: list[str] = []
    for line in lines[start + 1:]:
        if not line.startswith("      - "):
            break
        out.append(line.strip()[2:].strip('"'))
    return out


def _services(content: str) -> set[str]:
    import re

    body = content.split("\nservices:\n", 1)[1]
    body = re.split(r"\n(?=\S)", body, maxsplit=1)[0]
    return set(re.findall(r"^  ([a-z][a-z0-9_-]*):\s*$", body, re.M))


def _rendered_template() -> str:
    from agento.framework.cli._templates import get_template

    return render_compose(
        get_template("docker-compose.yml"),
        python_version="3.12",
        extensions=[],
        sandbox_packages=[],
    )


_DEV_COMPOSE = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.dev.yml"


def _compose_sources() -> list:
    sources = [pytest.param(_rendered_template, id="template")]
    if _DEV_COMPOSE.is_file():
        sources.append(pytest.param(_DEV_COMPOSE.read_text, id="dev"))
    return sources


@pytest.mark.parametrize("load", _compose_sources())
class TestPlatformFoundationServices:
    """E1.5 §2: the proxy is the only route to artifact files and the only bridge
    between agento-net and the artifacts `default` network."""

    def test_runs_the_seven_services(self, load):
        assert _services(load()) == {
            "sandbox", "toolbox", "cron", "artifacts", "mysql", "web", "proxy",
        }

    def test_only_the_proxy_bridges_both_networks(self, load):
        content = load()
        assert set(_items(_service_block(content, "proxy"), "networks")) == {"agento-net", "default"}
        for name in _services(content) - {"proxy"}:
            assert "default" not in _items(_service_block(content, name), "networks"), name

    def test_artifacts_has_no_network_and_no_host_port(self, load):
        block = _service_block(load(), "artifacts")
        assert "networks:" not in block
        assert "ports:" not in block

    def test_web_sits_on_agento_net_only(self, load):
        assert _items(_service_block(load(), "web"), "networks") == ["agento-net"]

    def test_only_proxy_and_web_mount_the_proxy_secret(self, load):
        content = load()
        mounting = {
            name for name in _services(content)
            if any(m.startswith("proxy-internal:") for m in _items(_service_block(content, name), "volumes"))
        }
        assert mounting == {"proxy", "web"}
        assert any(m.endswith(":ro") for m in _items(_service_block(content, "web"), "volumes")
                   if m.startswith("proxy-internal:"))

    def test_proxy_publishes_on_loopback_only(self, load):
        ports = _items(_service_block(load(), "proxy"), "ports")
        assert ports == ["127.0.0.1:${AGENTO_PROXY_PORT:-8443}:443"]

    def test_web_knows_the_browser_origins_with_the_proxy_defaults(self, load):
        content = load()
        env = _items(_service_block(content, "web"), "environment")
        assert {
            "AGENTO_PANEL_HOST=${AGENTO_PANEL_HOST:-panel.localhost}",
            "AGENTO_APPS_HOST=${AGENTO_APPS_HOST:-apps.localhost}",
            "AGENTO_PROXY_PORT=${AGENTO_PROXY_PORT:-8443}",
        } <= set(env)
        proxy_env = set(_items(_service_block(content, "proxy"), "environment"))
        assert {e for e in env if e.startswith("AGENTO_") and "_HOST=" in e} <= proxy_env

    def test_web_reads_the_module_inventory_read_only(self, load):
        volumes = _items(_service_block(load(), "web"), "volumes")
        assert {"../app/code:/app/code:ro", "../app/etc:/app/etc:ro"} <= set(volumes)

    def test_web_holds_no_secret(self, load):
        block = _service_block(load(), "web")
        assert "env_file:" not in block
        assert "AGENTO_ENCRYPTION_KEY" not in block

    def test_web_healthcheck_needs_no_curl(self, load):
        block = _service_block(load(), "web")
        assert "healthcheck:" in block
        assert "urllib.request" in block
        assert "curl" not in block


def test_web_mounts_the_same_extensions_as_cron():
    from agento.framework.cli._templates import get_template

    content = render_compose(get_template("docker-compose.yml"), python_version="3.12",
                             extensions=["acme_ext"], sandbox_packages=[])
    mount = "../.venv/lib/python3.12/site-packages/acme_ext:/opt/agento-src/acme_ext:ro"
    assert mount in _items(_service_block(content, "web"), "volumes")
    assert mount in _items(_service_block(content, "cron"), "volumes")
