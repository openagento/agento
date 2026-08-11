"""Project root detection and utilities for agento CLI."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Re-export (redundant alias marks it intentional): the implementation is framework-level so
# framework modules can use it without importing from `framework/cli/`.
from ..project import find_project_root as find_project_root
from ._output import log_error


def find_compose_file(project_root: Path) -> Path | None:
    """Find docker-compose file relative to project root.

    Checks (first match wins):
    1. docker/docker-compose.yml (customer install or legacy dev)
    2. docker/docker-compose.dev.yml (dev mode)
    3. docker-compose.yml (init'd project — written by agento install)
    """
    for candidate in [
        project_root / "docker" / "docker-compose.yml",
        project_root / "docker" / "docker-compose.dev.yml",
        project_root / "docker-compose.yml",
    ]:
        if candidate.is_file():
            return candidate
    return None


def find_override_file(project_root: Path) -> Path | None:
    """Find docker-compose.override.yml next to the active base compose file.

    Returns None when no base is present (a stray override without a base is
    meaningless) or when no override sits next to the base.
    """
    base = find_compose_file(project_root)
    if base is None:
        return None
    candidate = base.parent / "docker-compose.override.yml"
    return candidate if candidate.is_file() else None


def compose_file_flags(project_root: Path) -> list[str]:
    """Return the -f flag list for `docker compose`, merging override if present.

    Docker Compose auto-merges docker-compose.override.yml only when invoked
    without any -f. The agento CLI always passes -f explicitly, so we must
    list the override ourselves.

    Returns [] when no base compose file is found.
    """
    base = find_compose_file(project_root)
    if base is None:
        return []
    flags = ["-f", str(base)]
    override = find_override_file(project_root)
    if override is not None:
        flags += ["-f", str(override)]
    return flags


def resolve_host_ids() -> tuple[int, int]:
    """Detect the current process UID/GID. Refuse running as root.

    The container `agent` user is created at image build time with these IDs
    so bind-mounted host paths are writable by the unprivileged in-container
    user. UID 0 would defeat that model — bake root into the image, then
    every file the container writes is host-root-owned. Refuse instead and
    tell the operator to re-run as the project tree owner.
    """
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        log_error(
            "Refusing to run as root. Re-run as the non-root host user "
            "that owns the project tree — that UID/GID is baked into the "
            "container `agent` user."
        )
        sys.exit(1)
    return uid, gid


def update_dotenv_value(path: Path, key: str, value: str) -> None:
    """Update a single key in a .env file, preserving all other content.

    If the key exists, its value is replaced. If not, the key=value is appended.
    Comments and blank lines are preserved.
    """
    lines = path.read_text().splitlines(keepends=True)
    pattern = re.compile(rf"^{re.escape(key)}=")
    found = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    path.write_text("".join(lines))
