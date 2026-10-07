"""PRD E6 §9.1: AGENTO_SHARE_HOST unset keeps the local default; set to empty turns shares
off. Both compose files, both services that read it (proxy, toolbox)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agento.framework.cli._provisioning import render_compose

ROOT = Path(__file__).parents[4]
TEMPLATE = ROOT / "src" / "agento" / "framework" / "cli" / "templates" / "docker-compose.yml"
DEV = ROOT / "docker" / "docker-compose.dev.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="needs docker compose",
)


def _compose_files(tmp_path: Path) -> list[Path]:
    rendered = tmp_path / "template" / "docker-compose.yml"
    rendered.parent.mkdir()
    rendered.write_text(render_compose(TEMPLATE.read_text(), python_version="3.12", extensions=[], sandbox_packages=[]))
    dev = tmp_path / "dev" / "docker-compose.dev.yml"
    dev.parent.mkdir()
    shutil.copy(DEV, dev)
    (dev.parent / ".toolbox.env").write_text("")
    return [rendered, dev]


def _share_host(compose: Path, value: str | None) -> dict[str, str | None]:
    env = {k: v for k, v in os.environ.items() if k != "AGENTO_SHARE_HOST"}
    if value is not None:
        env["AGENTO_SHARE_HOST"] = value
    out = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--format", "json"],
                         env=env, capture_output=True, text=True, check=True, cwd=compose.parent).stdout
    services = json.loads(out)["services"]
    return {name: services[name]["environment"].get("AGENTO_SHARE_HOST") for name in ("proxy", "toolbox")}


def test_unset_keeps_the_default_and_empty_turns_shares_off(tmp_path):
    for compose in _compose_files(tmp_path):
        assert _share_host(compose, None) == {"proxy": "share.localhost", "toolbox": "share.localhost"}, compose
        assert _share_host(compose, "") == {"proxy": "", "toolbox": ""}, compose
        assert _share_host(compose, "s.example.com") == {"proxy": "s.example.com", "toolbox": "s.example.com"}


def test_toolbox_gets_the_same_panel_origin_as_web(tmp_path):
    """create_draft hands the agent the panel origin; it must be the one web checks."""
    keys = ("AGENTO_PANEL_HOST", "AGENTO_PROXY_PORT")
    env = {k: v for k, v in os.environ.items() if k not in keys}
    for compose in _compose_files(tmp_path):
        out = subprocess.run(["docker", "compose", "-f", str(compose), "config", "--format", "json"],
                             env=env, capture_output=True, text=True, check=True, cwd=compose.parent).stdout
        services = json.loads(out)["services"]
        web, toolbox = (services[n]["environment"] for n in ("web", "toolbox"))
        assert {k: toolbox.get(k) for k in keys} == {k: web.get(k) for k in keys}, compose
