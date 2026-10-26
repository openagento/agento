"""PRD E6 §9.1: AGENTO_SHARE_HOST unset keeps the local default; set to empty turns shares
off. Both compose files, both services that read it (proxy, toolbox)."""
from __future__ import annotations

import pytest

from tests.unit.framework.docker.compose_files import (
    compose_config,
    compose_sources,
    needs_compose,
    write_compose,
)

pytestmark = [needs_compose(), pytest.mark.parametrize("load", compose_sources())]


@pytest.mark.parametrize(("value", "expected"), [
    (None, "share.localhost"),
    ("", ""),
    ("s.example.com", "s.example.com"),
], ids=["unset-keeps-default", "empty-turns-off", "set"])
def test_share_host_reaches_proxy_and_toolbox(tmp_path, load, value, expected):
    env = {} if value is None else {"AGENTO_SHARE_HOST": value}
    services = compose_config(write_compose(tmp_path, load()), env)["services"]

    assert {n: services[n]["environment"].get("AGENTO_SHARE_HOST") for n in ("proxy", "toolbox")} \
        == {"proxy": expected, "toolbox": expected}


def test_toolbox_gets_the_same_panel_origin_as_web(tmp_path, load):
    """create_draft hands the agent the panel origin; it must be the one web checks."""
    keys = ("AGENTO_PANEL_HOST", "AGENTO_PROXY_PORT")
    services = compose_config(write_compose(tmp_path, load()))["services"]
    web, toolbox = (services[n]["environment"] for n in ("web", "toolbox"))

    assert {k: toolbox.get(k) for k in keys} == {k: web.get(k) for k in keys}
