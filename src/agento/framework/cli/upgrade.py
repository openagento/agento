"""agento upgrade — upgrade CLI + rebuild Docker images locally.

Customer projects own their ``pyproject.toml`` (composer.json equivalent).
``agento upgrade`` bumps the ``agento-core`` pin, runs ``uv sync`` to refresh
``.venv`` + ``uv.lock``, re-materializes the Docker build context, regenerates
the managed ``docker-compose.yml``, then rebuilds local images.

No GHCR pulls — images are built locally from the in-package context.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from ._env import parse_env_file
from ._output import log_error, log_info, log_warn
from ._project import (
    compose_file_flags,
    find_compose_file,
    find_project_root,
    resolve_host_ids,
    update_dotenv_value,
)
from ._provisioning import (
    build_base_images,
    bump_agento_version,
    ensure_storage_dirs,
    enumerate_sandbox_packages,
    find_links_for_local_install,
    materialize_docker_context,
    parse_semver_floor,
    regenerate_compose,
    write_project_pyproject,
)
from ._templates import get_package_version


def _fetch_latest_pypi_version() -> str | None:
    """Query PyPI for the latest agento-core version."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
            "https://pypi.org/pypi/agento-core/json", timeout=10,
        ) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None


def _upgrade_cli(version: str | None) -> str | None:
    """Upgrade the agento-core CLI package. Returns installed version or None on failure."""
    spec = f"agento-core=={version}" if version else "agento-core"

    log_info(f"Upgrading CLI ({spec})...")
    result = subprocess.run(
        ["uv", "tool", "install", "--upgrade", spec],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "is already installed" in (result.stdout + result.stderr):
            log_info("CLI already at target version.")
            return version or get_package_version()
        log_warn(f"CLI upgrade failed: {stderr}")
        return None

    # Extract installed version from uv output
    installed_version = None
    for line in (result.stdout + result.stderr).splitlines():
        if "agento-core" in line and "==" in line:
            for part in line.split():
                if part.startswith("agento-core=="):
                    ver = part.split("==")[1]
                    # Prefer '+' lines (newly installed) over '-' lines (removed)
                    if line.lstrip().startswith("+"):
                        return ver
                    installed_version = ver
    return installed_version or version or get_package_version()


def _backfill_or_warn_cli_pin(
    env_path: Path,
    existing: dict[str, str],
    *,
    key: str,
    default: str,
    display: str,
) -> None:
    """Backfill a missing CLI version pin, or warn when the existing one is older.

    Stale-pin policy is sticky-by-default: never overwrite a customer's value
    (they may have a reason — security pin, test version, downgrade). Only log
    a warning so they can decide whether to bump.
    """
    if key not in existing:
        update_dotenv_value(env_path, key, default)
        log_info(f"{key} backfilled to {default}")
        return

    current = parse_semver_floor(existing[key])
    target = parse_semver_floor(default)
    if current is not None and target is not None and current < target:
        log_warn(
            f"{key}={existing[key]} is older than this release's tested default "
            f"{default}. To bump: edit docker/.env and rebuild "
            f"({display} pin)."
        )


class UpgradeCommand:
    @property
    def name(self) -> str:
        return "upgrade"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Upgrade CLI + rebuild Docker images locally"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--version",
            default=None,
            help="Target version (default: latest from PyPI)",
        )
        parser.add_argument(
            "--no-build",
            action="store_true",
            help="Skip 'docker compose build' (CI/automation use).",
        )
        parser.add_argument(
            "--no-restart",
            action="store_true",
            help="Skip 'docker compose up -d' after building.",
        )

    def execute(self, args: argparse.Namespace) -> None:
        # Resolve target version
        if args.version:
            version = args.version
        else:
            log_info("Checking latest version on PyPI...")
            version = _fetch_latest_pypi_version()
            if version is None:
                log_error("Could not fetch latest version from PyPI. Use --version to specify.")
                sys.exit(1)

        current = get_package_version()
        if version == current:
            log_info(f"CLI already at {version}.")
        else:
            installed = _upgrade_cli(version)
            if installed:
                log_info(f"CLI upgraded to {installed}.")
            else:
                log_warn("CLI upgrade failed. Continuing with project upgrade.")

        # Project upgrade
        project_root = find_project_root()
        if project_root is None:
            log_info(f"Not inside an agento project. CLI upgraded to {version}, skipping project upgrade.")
            return

        if find_compose_file(project_root) is None:
            log_error("docker-compose.yml not found.")
            sys.exit(1)

        env_path = project_root / "docker" / ".env"
        if not env_path.is_file():
            log_error("docker/.env not found. Is this an agento project?")
            sys.exit(1)

        update_dotenv_value(env_path, "AGENTO_VERSION", version)
        log_info(f"AGENTO_VERSION set to {version}")

        # Backfill HOST_UID/HOST_GID for deployments installed before the
        # pin landed. Never overwrite an existing value — ops may have
        # pinned different IDs intentionally. Refuse running as root for
        # the same reason install does: UID 0 in containers defeats the
        # unprivileged-user model.
        existing = parse_env_file(env_path)
        if "HOST_UID" not in existing or "HOST_GID" not in existing:
            host_uid, host_gid = resolve_host_ids()
            if "HOST_UID" not in existing:
                update_dotenv_value(env_path, "HOST_UID", str(host_uid))
                log_info(f"HOST_UID backfilled to {host_uid}")
            if "HOST_GID" not in existing:
                update_dotenv_value(env_path, "HOST_GID", str(host_gid))
                log_info(f"HOST_GID backfilled to {host_gid}")

        # Backfill agent CLI pins for deployments that predate them. Then
        # warn — but never overwrite — when a customer's existing pin is
        # older than this release's default. The customer may have left it
        # intentionally; we just surface the gap so they know they're behind
        # the version we just smoke-tested. The list of pins to manage comes
        # from each agent module's sandbox_packages declaration.
        for pkg in enumerate_sandbox_packages(project_root):
            _backfill_or_warn_cli_pin(
                env_path, existing,
                key=pkg.version_env_key,
                default=pkg.default_range,
                display=pkg.binary,
            )

        # Bump the agento-core pin in the project's pyproject.toml. If the
        # project predates the per-project pyproject layout, write a fresh one.
        project_pyproject = project_root / "pyproject.toml"
        if project_pyproject.is_file():
            bump_agento_version(project_pyproject, version)
        else:
            write_project_pyproject(project_root, project_root.name, version)

        # Resolve deps, refresh Docker context, regenerate compose.
        log_info("Resolving dependencies (uv sync)...")
        result = subprocess.run(
            ["uv", "sync", *find_links_for_local_install()], cwd=project_root
        )
        if result.returncode != 0:
            log_error("uv sync failed. Resolve the error and rerun 'agento upgrade'.")
            sys.exit(result.returncode)
        materialize_docker_context(project_root, force=True)
        ensure_storage_dirs(project_root)
        regenerate_compose(project_root)
        log_info("Refreshed docker-compose.yml + .agento/docker/")

        compose = ["docker", "compose", *compose_file_flags(project_root)]

        if args.no_build:
            log_info("Skipping image build (--no-build).")
        else:
            log_info("Rebuilding Docker images locally...")
            # Build managed agento-<service>:<version> tags directly first so
            # a docker-compose.override.yml that re-bases on the managed tag
            # (FROM agento-toolbox:${AGENTO_VERSION}) has a base to layer on.
            # Without this, compose merges the override's build: section over
            # ours and the managed Dockerfile is never built.
            build_base_images(project_root, version)
            result = subprocess.run([*compose, "build", "sandbox"])
            if result.returncode != 0:
                log_error("Failed to build sandbox image.")
                sys.exit(result.returncode)
            result = subprocess.run([*compose, "build", "toolbox", "cron"])
            if result.returncode != 0:
                log_error("Failed to build toolbox/cron images.")
                sys.exit(result.returncode)

        if args.no_restart:
            log_info("Skipping container restart (--no-restart).")
        else:
            log_info("Restarting containers...")
            result = subprocess.run([*compose, "up", "-d"])
            if result.returncode != 0:
                log_error("Failed to restart containers.")
                sys.exit(result.returncode)

        log_info(f"Upgraded to {version}. setup:upgrade runs automatically on container start.")
