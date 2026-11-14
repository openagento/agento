"""MySQL keeps binlogs 3 days by default; MYSQL_BINLOG_EXPIRE_SECONDS changes it. Both compose files."""
from __future__ import annotations

import pytest

from tests.unit.framework.docker.compose_files import (
    compose_config,
    compose_sources,
    needs_compose,
    write_compose,
)

pytestmark = [needs_compose(), pytest.mark.parametrize("load", compose_sources())]


@pytest.mark.parametrize(("env", "seconds"), [
    ({}, "259200"),
    ({"MYSQL_BINLOG_EXPIRE_SECONDS": "86400"}, "86400"),
], ids=["default", "set"])
def test_mysql_command(tmp_path, load, env, seconds):
    services = compose_config(write_compose(tmp_path, load()), env)["services"]

    assert services["mysql"]["command"] == [
        "--max-connections=600", f"--binlog-expire-logs-seconds={seconds}"]
