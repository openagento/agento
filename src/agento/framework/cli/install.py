"""agento install — interactive project installation wizard."""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ._env import parse_env_file
from ._output import cyan, log_error, log_info, log_warn
from ._project import compose_file_flags, resolve_host_ids, update_dotenv_value
from ._provisioning import (
    build_base_images,
    enumerate_sandbox_packages,
    find_links_for_local_install,
    materialize_docker_context,
    regenerate_compose,
    write_project_pyproject,
)
from ._templates import TemplateNotFoundError, extract_sql_files, get_package_version, get_template
from .terminal import select


def _sanitize_compose_name(name: str) -> str:
    """Sanitize a string for use as COMPOSE_PROJECT_NAME.

    Lowercases, replaces spaces/dots/underscores with hyphens,
    strips invalid characters, collapses consecutive hyphens.
    Falls back to 'agento' if result is empty.
    """
    name = name.lower()
    name = re.sub(r"[\s._]+", "-", name)
    name = re.sub(r"[^a-z0-9-]", "", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    return name or "agento"


def _generate_password() -> str:
    """Generate a random URL-safe password."""
    return secrets.token_urlsafe(24)


def _is_port_free(port: int) -> bool:
    """Check if a TCP port is available on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _detect_timezone() -> str:
    """Detect the system timezone as an Olson name (e.g., 'Europe/Warsaw').

    Parses the /etc/localtime symlink. Falls back to 'UTC'.
    """
    try:
        link = Path("/etc/localtime").resolve()
        parts = link.parts
        idx = parts.index("zoneinfo")
        return "/".join(parts[idx + 1:])
    except (ValueError, OSError):
        return "UTC"


def _scaffold(project_dir: Path, project_name: str, config: dict[str, str]) -> None:
    """Create project directory structure and write config files."""
    dirs = [
        ".agento",
        "app/code",
        "app/etc",
        "workspace/artifacts",
        "workspace/build",
        "workspace/theme",
        "logs",
        "tokens",
        "storage",
        "docker",
        "docker/sql",
    ]
    for d in dirs:
        (project_dir / d).mkdir(parents=True, exist_ok=True)

    # Write project.json
    project_meta = {
        "name": project_name,
        "version": "0.1.0",
        "created_at": datetime.now(UTC).isoformat(),
    }
    (project_dir / ".agento" / "project.json").write_text(
        json.dumps(project_meta, indent=2) + "\n"
    )

    # Write .gitignore
    try:
        gitignore = get_template("gitignore")
        (project_dir / ".gitignore").write_text(gitignore)
    except TemplateNotFoundError:
        (project_dir / ".gitignore").write_text(
            "# Agento project\n"
            ".venv/\n"
            ".agento/docker/\n"
            "logs/\n"
            "tokens/\n"
            "storage/\n"
            "secrets.env\n"
            "docker/.env\n"
            "docker/.cron.env\n"
            "docker/.toolbox.env\n"
            ".env\n"
            "__pycache__/\n"
            "*.pyc\n"
            "node_modules/\n"
            "workspace/artifacts/\n"
            "workspace/build/\n"
        )

    # Project pyproject.toml — composer.json equivalent (pinned agento-core dep).
    write_project_pyproject(
        project_dir,
        project_name=project_name,
        agento_version=config["agento_version"],
    )

    # User-owned docker-compose.override.yml — never overwritten on upgrade.
    # Managed docker-compose.yml is generated later by regenerate_compose()
    # after `uv sync` so we know the venv's Python version.
    try:
        override = get_template("docker-compose.override.yml")
        (project_dir / "docker" / "docker-compose.override.yml").write_text(override)
    except TemplateNotFoundError:
        pass

    # Extract SQL migration scripts from installed package
    with contextlib.suppress(Exception):
        extract_sql_files(project_dir / "docker" / "sql")

    # Render docker/.env from template. Sandbox CLI pins come from each agent
    # module's `sandbox_packages` di.json declaration — no hardcoded provider
    # list here. At fresh-install time only core modules contribute (project
    # filesystem doesn't exist yet).
    sandbox_packages = enumerate_sandbox_packages()
    pin_lines = "".join(
        f"{pkg.version_env_key}={pkg.default_range}\n" for pkg in sandbox_packages
    )
    config_with_pins = {**config, "sandbox_package_pins": pin_lines}
    try:
        env_template = get_template("env.example")
        env_content = env_template.format_map(config_with_pins)
        (project_dir / "docker" / ".env").write_text(env_content)
    except TemplateNotFoundError:
        lines = [
            f"COMPOSE_PROJECT_NAME={config['compose_project_name']}",
            f"AGENTO_VERSION={config['agento_version']}",
            f"MYSQL_ROOT_PASSWORD={config['mysql_root_password']}",
            f"MYSQL_PASSWORD={config['mysql_password']}",
            f"MYSQL_PORT={config['mysql_port']}",
            f"TZ={config['timezone']}",
            f"HOST_UID={config['host_uid']}",
            f"HOST_GID={config['host_gid']}",
            *(f"{pkg.version_env_key}={pkg.default_range}" for pkg in sandbox_packages),
            "# Set to 1 to disable LLM API calls (mocks agent output, for testing)",
            "DISABLE_LLM=0",
            "",
        ]
        (project_dir / "docker" / ".env").write_text("\n".join(lines))

    # Write secrets.env with auto-generated encryption key
    encryption_key = secrets.token_hex(32)
    (project_dir / "secrets.env").write_text(
        "# Agento secrets — DO NOT commit this file\n"
        "\n"
        f"AGENTO_ENCRYPTION_KEY={encryption_key}\n"
    )

    # Write secrets.env.example
    try:
        secrets_content = get_template("secrets.env.example")
        (project_dir / "secrets.env.example").write_text(secrets_content)
    except TemplateNotFoundError:
        (project_dir / "secrets.env.example").write_text(
            "# Agento secrets — DO NOT commit this file\n"
            "# Copy to secrets.env and fill in your values\n"
            "\n"
            "# Jira credentials (only needed if using Jira module)\n"
            "JIRA_USER=\n"
            "JIRA_TOKEN=\n"
            "JIRA_HOST=\n"
            "\n"
            "# Encryption key for config values\n"
            "AGENTO_ENCRYPTION_KEY=\n"
        )


def _run_uv_sync(project_dir: Path) -> bool:
    """Run `uv sync` in the project directory. Returns True on success."""
    log_info("Resolving dependencies (uv sync)...")
    result = subprocess.run(
        ["uv", "sync", *find_links_for_local_install()], cwd=project_dir
    )
    if result.returncode != 0:
        log_error(
            "uv sync failed. Ensure 'uv' is installed and rerun "
            "(or run 'uv sync' manually in the project directory)."
        )
        return False
    return True


def _provision_project(project_dir: Path, *, force: bool = False) -> bool:
    """Run uv sync, materialize Docker context, regenerate compose.

    Returns True if all steps succeeded. Idempotent except for ``force``
    which forces re-materialization of the Docker context.
    """
    if not _run_uv_sync(project_dir):
        return False
    materialize_docker_context(project_dir, force=force)
    regenerate_compose(project_dir)
    return True


def _reinstall(project_dir: Path, host_uid: int, host_gid: int) -> None:
    """Reinstall framework files while preserving data.

    Preserves: storage/, tokens/, secrets.env, app/code/, workspace/,
    docker/.env passwords, project pyproject.toml version pin.
    Refreshes: docker-compose.yml, docker/sql/, .agento/docker/ context,
    .agento/project.json version, AGENTO_VERSION.
    Backfills HOST_UID/HOST_GID into docker/.env if missing (never overwrites
    existing values — ops may have pinned different IDs intentionally).
    """
    version = get_package_version()

    # Update AGENTO_VERSION in .env (preserve passwords, port, timezone)
    env_path = project_dir / "docker" / ".env"
    if env_path.is_file():
        update_dotenv_value(env_path, "AGENTO_VERSION", version)
        existing = parse_env_file(env_path)
        if "HOST_UID" not in existing:
            update_dotenv_value(env_path, "HOST_UID", str(host_uid))
        if "HOST_GID" not in existing:
            update_dotenv_value(env_path, "HOST_GID", str(host_gid))
        # Backfill agent CLI pins for projects that predate the pin landing.
        # Never overwrite — a customer may have intentionally bumped to a newer
        # tested version ahead of the agento default. The set of pins to
        # backfill comes from each agent module's sandbox_packages declaration.
        for pkg in enumerate_sandbox_packages(project_dir):
            if pkg.version_env_key not in existing:
                update_dotenv_value(env_path, pkg.version_env_key, pkg.default_range)
    else:
        log_warn("docker/.env not found — skipping version update.")

    # Bump the agento-core pin in <project>/pyproject.toml so `uv sync`
    # pulls the matching framework version.
    project_pyproject = project_dir / "pyproject.toml"
    if project_pyproject.is_file():
        from ._provisioning import bump_agento_version
        bump_agento_version(project_pyproject, version)
    else:
        write_project_pyproject(project_dir, project_dir.name, version)

    # Refresh user-owned override stub if missing (never overwrite existing).
    override_path = project_dir / "docker" / "docker-compose.override.yml"
    if not override_path.is_file():
        try:
            override = get_template("docker-compose.override.yml")
            override_path.write_text(override)
        except TemplateNotFoundError:
            pass

    # Re-sync deps, re-materialize Docker context, regenerate compose.
    _provision_project(project_dir, force=True)

    # Refresh SQL migration scripts
    with contextlib.suppress(Exception):
        extract_sql_files(project_dir / "docker" / "sql")

    # Update project.json version
    project_json = project_dir / ".agento" / "project.json"
    if project_json.is_file():
        meta = json.loads(project_json.read_text())
        meta["version"] = version
        project_json.write_text(json.dumps(meta, indent=2) + "\n")

    log_info(f"Reinstalled framework files (version {version}).")


def _run_post_install(project_dir: Path) -> None:
    """Build images, run agento up + setup:upgrade after scaffolding."""
    flags = compose_file_flags(project_dir)
    if not flags:
        log_warn("docker-compose.yml not found. Skipping runtime startup.")
        return

    compose_cmd = ["docker", "compose", *flags]

    # Build managed agento-<service>:<version> tags directly first so a
    # docker-compose.override.yml that re-bases on the managed tag
    # (FROM agento-toolbox:${AGENTO_VERSION}) has a base to layer on.
    log_info("Building Docker images (first run can take a few minutes)...")
    build_base_images(project_dir, get_package_version())
    result = subprocess.run([*compose_cmd, "build", "sandbox"])
    if result.returncode != 0:
        log_error("Failed to build sandbox image. Run 'docker compose build' manually.")
        return
    result = subprocess.run([*compose_cmd, "build", "toolbox", "cron"])
    if result.returncode != 0:
        log_error("Failed to build toolbox/cron images. Run 'docker compose build' manually.")
        return

    log_info("Starting containers...")
    result = subprocess.run([*compose_cmd, "up", "-d"])
    if result.returncode != 0:
        log_error("Failed to start containers. Run 'agento up' manually.")
        return

    # The cron entrypoint runs setup:upgrade --skip-onboarding on start and
    # touches /tmp/.setup-done when finished.  Wait for that before running
    # the interactive setup:upgrade (which only triggers onboarding — migrations
    # are already applied).
    log_info("Waiting for initial setup...")
    for _ in range(60):
        check = subprocess.run(
            [*compose_cmd, "exec", "-u", "agent", "-T", "cron", "test", "-f", "/tmp/.setup-done"],
            capture_output=True,
        )
        if check.returncode == 0:
            break
        time.sleep(2)
    else:
        log_warn("setup:upgrade timed out. Run 'agento setup:upgrade' manually.")
        return

    log_info("Running setup:upgrade...")
    result = subprocess.run(
        [*compose_cmd, "exec", "-u", "agent", "-it", "cron", "/opt/cron-agent/run.sh", "setup:upgrade"],
    )
    if result.returncode != 0:
        log_warn("setup:upgrade failed. Run 'agento setup:upgrade' manually.")
        return

    _setup_agent_harness(compose_cmd, project_dir)


# How each declared registration mode is requested on the command line. Interactive OAuth
# takes no flag; the secret-based modes read the secret from stdin/getpass.
_MODE_FLAGS = {
    "interactive_oauth": "",
    "api_key": "--with-api-key",
    "access_token": "--with-access-token",
}


def _registration_flag(provider) -> str | None:
    """The ``credential:register`` flag for this provider's preferred declared mode.

    Returns ``""`` for interactive OAuth (no flag), a flag string for a secret-based mode,
    or ``None`` when the provider declares nothing this wizard knows how to drive. Driving
    OAuth unconditionally would fail onboarding for a provider that only accepts a pasted
    secret — which is exactly what ``registration_modes`` exists to express.
    """
    from ..harness import CredentialRegistrationMode

    preference = (
        CredentialRegistrationMode.INTERACTIVE_OAUTH,
        CredentialRegistrationMode.API_KEY,
        CredentialRegistrationMode.ACCESS_TOKEN,
    )
    declared = set(provider.registration_modes)
    for mode in preference:
        if mode in declared:
            return _MODE_FLAGS[mode.value]
    return None


def _setup_agent_harness(compose_cmd: list[str], project_dir: Path | None = None) -> None:
    """Pick a harness + provider from the declarations and bind both config paths.

    Descriptor-driven: the options come from the enabled modules' ``agent_harnesses``
    entries (read off disk — no bootstrap here), so a harness shipped by an ``app/code``
    or PyPI module appears without editing this wizard. A provider that declares
    ``credential_required: false`` skips registration entirely.
    """
    from ..harness import enumerate_harness_declarations

    try:
        declarations = enumerate_harness_declarations(project_dir)
    except Exception as exc:
        log_warn(f"Could not enumerate agent harnesses ({exc}). Configure manually.")
        return
    if not declarations:
        return

    from .terminal import select

    # One entry per (harness, provider) pair — the pair is what a run needs.
    pairs = [
        (d.descriptor, provider)
        for d in declarations
        for provider in d.descriptor.providers
    ]
    labels = [
        f"{desc.label} / {provider.label}"
        + ("" if provider.credential_required else " (no credential needed)")
        for desc, provider in pairs
    ]
    choice = select("Choose agent harness and provider:", [*labels, "Skip (configure later)"])
    if choice >= len(pairs):
        return

    descriptor, provider = pairs[choice]
    harness_id, provider_id = str(descriptor.id), str(provider.id)

    if provider.credential_required:
        scope = str(provider.credential_scope)
        mode_flag = _registration_flag(provider)
        if mode_flag is None:
            log_warn(
                f"Provider {provider_id!r} declares no usable registration mode; "
                f"register manually: agento credential:register {scope} <label>"
            )
            return
        log_info(f"Registering {scope} credential ({mode_flag or 'interactive OAuth'})...")
        result = subprocess.run(
            [*compose_cmd, "exec", "-u", "agent", "-it", "cron",
             "/opt/cron-agent/run.sh", "credential:register", scope, "default",
             *([mode_flag] if mode_flag else [])],
        )
        if result.returncode != 0:
            log_warn(
                f"Credential registration failed. Run "
                f"'agento credential:register {scope} <label>"
                f"{' ' + mode_flag if mode_flag else ''}' manually."
            )
            return

    # Both paths: the harness alone does not identify the model vendor. Credential
    # selection is LRU over the healthy pool, so nothing else needs binding.
    for path, value in (
        ("agent_view/harness", harness_id),
        ("agent_view/provider", provider_id),
    ):
        subprocess.run(
            [*compose_cmd, "exec", "-u", "agent", "-T", "cron",
             "/opt/cron-agent/run.sh", "config:set", path, value],
        )
    log_info(f"Agent harness set to: {harness_id} / {provider_id}")


class InstallCommand:
    @property
    def name(self) -> str:
        return "install"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Install a new agento project (interactive wizard)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        # Resolve host UID/GID once. Refuses if running as root — containers
        # build the `agent` user with these IDs so bind-mounted host paths
        # are writable without root.
        host_uid, host_gid = resolve_host_ids()

        # Step 1: Ask project path
        project_dir = self._ask_project_path()
        project_name = project_dir.name

        # Check if already installed — offer reinstall
        if (project_dir / ".agento" / "project.json").is_file():
            print()
            print("This project is already installed. Reinstalling will refresh")
            print("framework files while preserving:")
            print("  - storage/    (MySQL data)")
            print("  - tokens/")
            print("  - secrets.env")
            print("  - app/code/   (user modules)")
            print("  - workspace/")
            print()
            choice = select("Proceed with reinstall?", ["Yes", "No"])
            if choice == 1:  # No
                return
            _reinstall(project_dir, host_uid, host_gid)
            _run_post_install(project_dir)
            return

        # Validate directory
        if project_dir.exists():
            if any(project_dir.iterdir()):
                log_error(f"Directory is not empty: {project_dir}")
                sys.exit(1)
        else:
            project_dir.mkdir(parents=True)

        # Step 2: Ask install mode
        mode = select("Installation mode:", [
            "Basic (recommended)",
            "Advanced",
        ])

        # Collect config
        compose_name = _sanitize_compose_name(project_name)
        mysql_port = "3306"
        timezone = _detect_timezone()

        if mode == 1:  # Advanced
            compose_name = self._ask_compose_name(compose_name)
            mysql_port = self._ask_mysql_port()
            timezone = self._ask_timezone(timezone)

        config = {
            "compose_project_name": compose_name,
            "agento_version": get_package_version(),
            "mysql_root_password": _generate_password(),
            "mysql_password": _generate_password(),
            "mysql_port": mysql_port,
            "timezone": timezone,
            "host_uid": str(host_uid),
            "host_gid": str(host_gid),
        }

        # Scaffold
        log_info(f"Installing agento project: {project_name}")
        _scaffold(project_dir, project_name, config)
        log_info(f"Project created at: {project_dir}")

        # Resolve deps + materialize Docker context + render compose.
        # Failure here is fatal: containers can't start without it.
        if not _provision_project(project_dir, force=True):
            log_error("Provisioning failed. Fix the error above and rerun 'agento install'.")
            sys.exit(1)

        # Post-install: build images + start runtime
        _run_post_install(project_dir)

        print()
        print(f"{cyan('Next steps:')}")
        print("  agento module:add <name>      Add your first module")
        print("  agento credential:register <scope> <label>   Register an agent credential")
        print("  agento logs                    View container logs")
        print()

    def _ask_project_path(self) -> Path:
        """Prompt for project path with validation."""
        while True:
            raw = input("  Project path [.]: ").strip()
            if not raw:
                raw = "."
            project_dir = (Path.cwd() / raw).resolve()
            if project_dir.exists() and not project_dir.is_dir():
                log_error(f"Not a directory: {project_dir}")
                continue
            return project_dir

    def _ask_compose_name(self, default: str) -> str:
        """Prompt for COMPOSE_PROJECT_NAME with sanitization."""
        while True:
            raw = input(f"  Docker project name [{default}]: ").strip()
            if not raw:
                return default
            sanitized = _sanitize_compose_name(raw)
            if sanitized != raw.lower():
                log_info(f"Sanitized to: {sanitized}")
            return sanitized

    def _ask_mysql_port(self) -> str:
        """Prompt for MySQL port with validation."""
        while True:
            raw = input("  MySQL host port [3306]: ").strip()
            if not raw:
                raw = "3306"
            try:
                port = int(raw)
            except ValueError:
                log_error("Invalid port number.")
                continue
            if not (1 <= port <= 65535):
                log_error("Port must be between 1 and 65535.")
                continue
            if not _is_port_free(port):
                log_error(f"Port {port} is already in use.")
                continue
            return str(port)

    def _ask_timezone(self, default: str) -> str:
        """Prompt for timezone."""
        raw = input(f"  Timezone [{default}]: ").strip()
        return raw if raw else default
