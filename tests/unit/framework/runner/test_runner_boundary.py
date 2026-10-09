"""The runner boundary (WS6, SEC-1, SEC-9): the runner never gets the store. "cron spawns
nothing" is ``test_spawn_guard.py``. No docker needed."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agento.framework.cli._provisioning import render_compose

ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = ROOT / "src/agento/framework/cli/templates/docker-compose.yml"
DEV = ROOT / "docker/docker-compose.dev.yml"
ENTRYPOINT = (ROOT / "src/agento/framework/docker/cron/entrypoint.sh").read_text()
STORE = re.compile(r"MYSQL_|AGENTO_ENCRYPTION_KEY|CONFIG__|PASSWORD|secrets\.env")


def _sources():
    for count in (1, 2):
        yield pytest.param(render_compose(TEMPLATE.read_text(), python_version="3.12",
                                          extensions=[], sandbox_packages=[],
                                          runner_count=count), count, id=f"template-{count}")
    yield pytest.param(DEV.read_text(), 1, id="dev")


@pytest.mark.parametrize(("content", "count"), _sources())
def test_a_runner_service_holds_no_store(content, count):
    blocks = dict(re.findall(r"^  (runner-\d+):\n((?:(?:    .*)?\n)*)", content, re.M))
    assert sorted(blocks) == [f"runner-{i}" for i in range(1, count + 1)]
    for name, block in blocks.items():
        assert "env_file" not in block, name
        assert not STORE.search(block), name
        assert re.search(r"^    pid:", block, re.M) is None, name  # A5: own PID namespace


def test_the_runner_starts_after_the_ssh_guard_and_before_the_env_file_is_written():
    """The runner never writes the file that holds the worker's MYSQL_* and key."""
    runner_at = ENTRYPOINT.index('= "runner"')
    assert ENTRYPOINT.index("AGENTO_SSH_PRIVATE_KEY") < runner_at
    assert ENTRYPOINT.index("/opt/cron-agent/env") > runner_at

