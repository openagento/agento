"""Runs docker/smoke/proxy-smoke.sh against this checkout's running dev stack."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "docker" / "smoke" / "proxy-smoke.sh"


def _project() -> str:
    env_file = _ROOT / "docker" / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("COMPOSE_PROJECT_NAME="):
                return line.split("=", 1)[1].strip()
    return "agento"


def _proxy_running(project: str) -> bool:
    if shutil.which("docker") is None:
        return False
    out = subprocess.run(
        ["docker", "ps", "-q",
         "--filter", f"label=com.docker.compose.project={project}",
         "--filter", "label=com.docker.compose.service=proxy"],
        capture_output=True, text=True,
    )
    return out.returncode == 0 and bool(out.stdout.strip())


_E2E = os.environ.get("AGENTO_E2E") == "1"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _E2E, reason="set AGENTO_E2E=1 to run"),
    pytest.mark.skipif(
        _E2E and not _proxy_running(_project()),
        reason="no running proxy — docker compose -f docker/docker-compose.dev.yml up -d web proxy",
    ),
]


def test_proxy_smoke_passes():
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--project", _project()],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
