"""The runner-<i> services (WS5, docs/architecture/runner.md): rendered from
AGENTO_RUNNER_COUNT, with no secret and no shared PID namespace (SEC-1, SEC-12)."""
from __future__ import annotations

import pytest

from agento.framework.cli import _provisioning
from tests.unit.framework.docker.compose_files import (
    compose_config,
    needs_compose,
    render_template,
    write_compose,
)

RUNNER_CAPS = ["SETUID", "SETGID", "CHOWN", "FOWNER", "FSETID", "DAC_READ_SEARCH", "KILL"]


@needs_compose()
@pytest.mark.parametrize("count", [1, 2])
def test_one_service_per_runner_wired_to_the_worker(tmp_path, count):
    """No secret and no shared PID namespace: runner/test_runner_boundary.py."""
    path = write_compose(tmp_path, render_template(extensions=["ext"], runner_count=count))
    config = compose_config(path, {"MYSQL_BACKEND_PASSWORD": "x", "MYSQL_TOOLBOX_PASSWORD": "y"})
    services = config["services"]
    runners = sorted(n for n in services if n.startswith("runner-"))
    cron = services["cron"]
    cron_workspace = next(v for v in cron["volumes"] if v["target"] == "/workspace")

    web = services["web"]
    toolbox = services["toolbox"]

    assert runners == [f"runner-{i}" for i in range(1, count + 1)]
    assert cron["environment"]["AGENTO_RUNNER_COUNT"] == str(count)
    # web runs a local config tester (C1): its CLI starts in a runner, HOME on /workspace.
    assert web["environment"]["AGENTO_RUNNER_COUNT"] == str(count)
    assert next(v for v in web["volumes"] if v["target"] == "/workspace")["source"] == cron_workspace["source"]
    # The toolbox gets every run uid's group (its entrypoint reads the count).
    assert toolbox["environment"]["AGENTO_RUNNER_COUNT"] == str(count)
    assert set(runners) <= set(cron["depends_on"])
    # SEC-7: another runner's agent is pid 0 too, so each socket has its own volume.
    mounts = {n: {v.get("source") for v in s.get("volumes", [])} for n, s in services.items()}
    for i, name in enumerate(runners, 1):
        runner = services[name]
        assert runner["environment"]["AGENTO_RUNNER_SOCKET"] == f"/run/agento-runner/runner-{i}.sock"
        workspace = next(v for v in runner["volumes"] if v["target"] == "/workspace")
        assert workspace["source"] == cron_workspace["source"]
        sock = next(v["source"] for v in runner["volumes"] if v["target"] == "/run/agento-runner")
        assert {n for n, m in mounts.items() if sock in m} == {"cron", "web", name}
        for backend in (cron, web):
            assert any(v["source"] == sock and v["target"] == f"/run/agento-runner/runner-{i}"
                       and v.get("read_only") for v in backend["volumes"])
        # A root server that only changes users, owners and signals (runner.md).
        assert runner["cap_drop"] == ["ALL"]
        assert sorted(runner["cap_add"]) == sorted(RUNNER_CAPS)
        assert "no-new-privileges:true" in runner["security_opt"]
        assert any(v["target"] == "/opt/agento-src/ext" for v in runner["volumes"])


def test_the_count_comes_from_docker_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTO_RUNNER_COUNT", raising=False)
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / ".env").write_text("AGENTO_RUNNER_COUNT=3\n")
    monkeypatch.setattr(_provisioning, "detect_python_version", lambda _venv: "3.12")
    monkeypatch.setattr(_provisioning, "enumerate_enabled_extensions", lambda _p: [])

    _provisioning.regenerate_compose(tmp_path)

    text = (tmp_path / "docker" / "docker-compose.yml").read_text()
    assert "  runner-3:\n" in text and "  runner-4:\n" not in text
    assert "{{" not in text
