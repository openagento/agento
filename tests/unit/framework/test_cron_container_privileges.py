"""Guards over the cron image: what runs as root, and what uid ``agent`` can write.

These assert the *shape* of the entrypoint and Dockerfile. No agent runs in cron (WS6:
``runner/test_spawn_guard.py``), so they defend the root-only programs and the crontab,
not a store hidden from a same-uid peer.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DOCKER_CRON = (
    Path(__file__).resolve().parents[3] / "src/agento/framework/docker/cron"
)
ENTRYPOINT = (DOCKER_CRON / "entrypoint.sh").read_text()
DOCKERFILE = (DOCKER_CRON / "Dockerfile").read_text()
LAUNCH_SH = (DOCKER_CRON / "launch.sh").read_text()


# --- the replaced boundary --------------------------------------------------------------

def test_the_entrypoint_never_drops_privileges_with_su():
    """``su -`` wipes the environment, which is the only reason a readable env file existed."""
    assert "su - agent" not in ENTRYPOINT
    assert "su -" not in ENTRYPOINT


def test_every_privilege_drop_goes_through_the_launcher():
    drops = [ln for ln in ENTRYPOINT.splitlines() if "/opt/cron-agent/run.sh" in ln]
    assert drops
    for line in drops:
        assert "/opt/cron-agent/launch.sh" in line


def test_the_env_file_is_root_only():
    """It holds MYSQL_* and the key. Created under umask 077, and an existing file from an
    older image is tightened too (the umask applies only at creation)."""
    assert "(umask 077; env -0 | grep -zE" in ENTRYPOINT
    assert "chmod 0600 /opt/cron-agent/env" in ENTRYPOINT


def test_the_agent_owns_nothing_under_opt_cron_agent():
    assert "chown -R agent /opt/cron-agent" not in DOCKERFILE
    assert "chown -R root:root /opt/cron-agent" in DOCKERFILE
    for program in ("launch.sh", "install-crontab.py"):
        assert f"/opt/cron-agent/{program}" in DOCKERFILE
    assert "chmod 0700" in DOCKERFILE


def test_the_agent_has_no_crontab():
    assert "crontab -u agent -" not in ENTRYPOINT.replace("crontab -u agent -r", "")
    assert "crontab -u agent -r" in ENTRYPOINT  # any pre-existing one is removed


def test_the_drop_uses_setpriv_without_reset_env():
    assert "setpriv" in LAUNCH_SH
    assert "--reset-env" not in LAUNCH_SH


def test_the_renderer_runs_after_setup_upgrade():
    """A fresh database has no ``schedule`` table until migrations have run."""
    setup_at = ENTRYPOINT.index("setup:upgrade --skip-onboarding")
    render_at = ENTRYPOINT.index("install-crontab.py", setup_at)
    assert render_at > setup_at


def test_the_bootstrap_installer_line_matches_the_renderers_own():
    """Root re-emits the line that invokes it; a drift would stop the renderer."""
    renderer_spec = importlib.util.spec_from_file_location(
        "ic_line", DOCKER_CRON / "install-crontab.py",
    )
    renderer = importlib.util.module_from_spec(renderer_spec)
    sys.modules["ic_line"] = renderer
    renderer_spec.loader.exec_module(renderer)
    assert renderer.INSTALLER_LINE in ENTRYPOINT
