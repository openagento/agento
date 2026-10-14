"""agento doctor — check system prerequisites."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from ._env import parse_env_file
from ._output import log_error
from ._project import EXEC_SERVICE, compose_file_flags, find_project_root
from ._provisioning import enumerate_sandbox_packages, parse_semver_floor


def _check_binary(name: str, version_args: list[str] | None = None) -> tuple[bool, str]:
    """Check if a binary is available and get its version."""
    path = shutil.which(name)
    if not path:
        return False, "not found"
    if version_args is None:
        version_args = [name, "--version"]
    try:
        result = subprocess.run(
            version_args,
            capture_output=True, text=True, timeout=10,
        )
        # Extract version from first line of output
        output = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr) else ""
        return True, output
    except (subprocess.TimeoutExpired, OSError):
        return True, "installed (version unknown)"


def _check_docker_compose() -> tuple[bool, str]:
    """Check Docker Compose V2."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, "not found"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False, "not found"


def _check_python() -> tuple[bool, str]:
    """Check Python version."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 11)
    return ok, f"Python {version}" + ("" if ok else " (requires >=3.11)")


def _check_sandbox_cli_pin(
    project_root: Path,
    *,
    binary: str,
    pin_key: str,
) -> tuple[bool, str]:
    """Compare the live ``<binary> --version`` in the runner to the docker/.env pin.

    Returns ``(ok, info)`` where ``ok`` reflects whether we could verify
    alignment. ``ok=True`` covers both "in range" and "no live data to check"
    (runner not running / project not initialised). ``ok=False`` is reserved
    for actual drift between the pin and what's installed inside the image,
    which usually means someone edited docker/.env without rebuilding.
    """
    env_path = project_root / "docker" / ".env"
    env = parse_env_file(env_path)
    pinned = env.get(pin_key)
    if not pinned:
        return True, f"{pin_key} not set in docker/.env"

    flags = compose_file_flags(project_root)
    if not flags:
        return True, f"pinned {pinned} (images not built yet)"

    try:
        result = subprocess.run(
            ["docker", "compose", *flags, "exec", "-T", EXEC_SERVICE, binary, "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True, f"pinned {pinned} (runner not running)"

    if result.returncode != 0:
        return True, f"pinned {pinned} (runner not running)"

    live = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr) else ""
    live_floor = parse_semver_floor(live)
    pin_floor = parse_semver_floor(pinned)
    if live_floor is None or pin_floor is None:
        return True, f"pinned {pinned}, live {live or 'unknown'}"
    # Accept any patch within the pinned minor (>=pin_floor, same major+minor).
    # Defaults are exact pins, so live should equal pin in practice; the window
    # is kept for back-compat with tilde-style customer pins. Anything outside
    # the window means someone edited the pin (or the Dockerfile ARG default
    # drifted) without rebuilding the images.
    in_range = (
        live_floor >= pin_floor
        and live_floor[0] == pin_floor[0]
        and live_floor[1] == pin_floor[1]
    )
    if in_range:
        return True, f"pinned {pinned}, live {live}"
    return False, (
        f"pinned {pinned}, live {live} — rebuild the images "
        "(agento upgrade --version <installed version>)"
    )


class DoctorCommand:
    @property
    def name(self) -> str:
        return "doctor"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Check system prerequisites"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        print("\nAgento Doctor\n")

        checks = []

        # Python
        ok, info = _check_python()
        checks.append(("Python", ok, info))

        # uv
        ok, info = _check_binary("uv")
        checks.append(("uv", ok, info))

        # Docker
        ok, info = _check_binary("docker")
        checks.append(("Docker", ok, info))

        # Docker Compose
        ok, info = _check_docker_compose()
        checks.append(("Docker Compose", ok, info))

        # Node.js
        ok, info = _check_binary("node")
        checks.append(("Node.js", ok, info))

        # npm
        ok, info = _check_binary("npm")
        checks.append(("npm", ok, info))

        # Agent CLI pins inside the runner (skipped silently outside a
        # project). The list of agents to check comes from each agent module's
        # sandbox_packages declaration — no hardcoded provider names here.
        project_root = find_project_root()
        if project_root is not None:
            for pkg in enumerate_sandbox_packages(project_root):
                ok, info = _check_sandbox_cli_pin(
                    project_root, binary=pkg.binary, pin_key=pkg.version_env_key,
                )
                checks.append((f"{pkg.binary} pin", ok, info))

        # Display results
        for name, ok, info in checks:
            status = "OK" if ok else "MISSING"
            print(f"  {name:20} {status:8} {info}")

        print()

        # Determine available modes
        has_docker = any(name == "Docker" and ok for name, ok, _ in checks)
        has_compose = any(name == "Docker Compose" and ok for name, ok, _ in checks)

        if has_docker and has_compose:
            print("  Docker Compose          ready")
        else:
            print("  Docker Compose          not available (install Docker + Compose)")

        print()

        # Exit code: 0 if Python >= 3.11
        python_ok = any(name == "Python" and ok for name, ok, _ in checks)
        if not python_ok:
            log_error("Python >= 3.11 is required.")
            sys.exit(1)
