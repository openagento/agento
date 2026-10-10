"""The runner-<i> services (WS5, docs/architecture/runner.md): rendered from
AGENTO_RUNNER_COUNT, with no secret and no shared PID namespace (SEC-1, SEC-12)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agento.framework.cli import _provisioning
from agento.framework.cli._provisioning import render_compose

ROOT = Path(__file__).parents[4]
TEMPLATE = ROOT / "src" / "agento" / "framework" / "cli" / "templates" / "docker-compose.yml"
HAS_COMPOSE = shutil.which("docker") is not None and subprocess.run(
    ["docker", "compose", "version"], capture_output=True).returncode == 0


def _services(compose: Path) -> dict:
    out = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--format", "json"],
                         env={**os.environ, "MYSQL_BACKEND_PASSWORD": "x", "MYSQL_TOOLBOX_PASSWORD": "y"},
                         capture_output=True, text=True, check=True, cwd=compose.parent).stdout
    return json.loads(out)


@pytest.mark.skipif(not HAS_COMPOSE, reason="needs docker compose")
@pytest.mark.parametrize("count", [1, 2])
def test_one_service_per_runner_wired_to_the_worker(tmp_path, count):
    """No secret and no shared PID namespace: runner/test_runner_boundary.py."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(render_compose(TEMPLATE.read_text(), python_version="3.12", extensions=["ext"],
                                   sandbox_packages=[], runner_count=count))
    config = _services(path)
    services = config["services"]
    runners = sorted(n for n in services if n.startswith("runner-"))
    cron = services["cron"]
    cron_workspace = next(v for v in cron["volumes"] if v["target"] == "/workspace")

    assert runners == [f"runner-{i}" for i in range(1, count + 1)]
    assert cron["environment"]["AGENTO_RUNNER_COUNT"] == str(count)
    assert set(runners) <= set(cron["depends_on"])
    # SEC-7: another runner's agent is pid 0 too, so each socket has its own volume.
    mounts = {n: {v.get("source") for v in s.get("volumes", [])} for n, s in services.items()}
    for i, name in enumerate(runners, 1):
        runner = services[name]
        assert runner["environment"]["AGENTO_RUNNER_SOCKET"] == f"/run/agento-runner/runner-{i}.sock"
        workspace = next(v for v in runner["volumes"] if v["target"] == "/workspace")
        assert workspace["source"] == cron_workspace["source"]
        sock = next(v["source"] for v in runner["volumes"] if v["target"] == "/run/agento-runner")
        assert {n for n, m in mounts.items() if sock in m} == {"cron", name}
        assert any(v["source"] == sock and v["target"] == f"/run/agento-runner/runner-{i}"
                   and v.get("read_only") for v in cron["volumes"])
        assert any(v["target"] == "/opt/agento-src/ext" for v in runner["volumes"])


def test_the_count_comes_from_docker_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTO_RUNNER_COUNT", raising=False)
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / ".env").write_text("AGENTO_RUNNER_COUNT=3\n")
    monkeypatch.setattr(_provisioning, "detect_python_version", lambda _venv: "3.12")
    monkeypatch.setattr(_provisioning, "enumerate_enabled_extensions", lambda _p: [])
    monkeypatch.setattr(_provisioning, "enumerate_sandbox_packages", lambda _p: [])

    _provisioning.regenerate_compose(tmp_path)

    text = (tmp_path / "docker" / "docker-compose.yml").read_text()
    assert "  runner-3:\n" in text and "  runner-4:\n" not in text
    assert "{{" not in text
