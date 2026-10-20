"""The two compose files the tests read (the rendered template and the dev file), and
``docker compose config`` on one of them with a hermetic env (TST-4)."""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agento.framework.cli._provisioning import render_compose
from agento.framework.cli._templates import get_template

DEV = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.dev.yml"


def render_template(*, extensions: list[str] | None = None, runner_count: int = 1) -> str:
    return render_compose(get_template("docker-compose.yml"), python_version="3.12",
                          extensions=extensions or [], runner_count=runner_count)


def compose_sources() -> list:
    """Loaders of each compose file, one ``pytest.param`` each (ids ``template``/``dev``)."""
    sources = [pytest.param(render_template, id="template")]
    if DEV.is_file():
        sources.append(pytest.param(DEV.read_text, id="dev"))
    return sources


@functools.cache
def has_compose() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "compose", "version"], capture_output=True).returncode == 0


def needs_compose():
    return pytest.mark.skipif(not has_compose(), reason="needs docker compose")


def write_compose(directory: Path, text: str) -> Path:
    """``text`` as a compose file in ``directory``, with the env file the dev file names."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".toolbox.env").write_text("")
    path = directory / "docker-compose.yml"
    path.write_text(text)
    return path


def compose_config(path: Path, env: dict[str, str] | None = None) -> dict:
    """``docker compose config`` as JSON. Only PATH and HOME come from the host: a host
    AGENTO_* value (a dev proxy port, a share host) never reaches the result."""
    base = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    out = subprocess.run(["docker", "compose", "-f", str(path), "config", "--format", "json"],
                         env={**base, **(env or {})}, capture_output=True, text=True, check=True,
                         cwd=path.parent).stdout
    return json.loads(out)
