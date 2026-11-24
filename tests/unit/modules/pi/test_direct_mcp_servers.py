"""Pi: the bridge's one URL is the only MCP server; direct when it is not our toolbox."""
from __future__ import annotations

import json

import pytest

from agento.framework.harness import detect_direct_mcp_servers
from agento.modules.pi.src.config import PiWorkspaceAdapter

TOOLBOX = "http://toolbox:3001"


@pytest.fixture
def adapter():
    return PiWorkspaceAdapter()


def _write(run_dir, payload: dict) -> None:
    (run_dir / ".pi").mkdir(exist_ok=True)
    (run_dir / ".pi" / "agento-toolbox.json").write_text(json.dumps(payload))


def test_remote_server_named_toolbox_is_direct(adapter, tmp_path):
    _write(tmp_path, {"url": "https://evil.example.com/mcp"})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["toolbox"]


def test_toolbox_endpoint_under_other_name_is_not_direct(adapter, tmp_path):
    _write(tmp_path, {"url": "http://toolbox:3001/mcp?job_id=4"})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == []


def test_stdio_entry_is_direct(adapter, tmp_path):
    _write(tmp_path, {"command": "npx"})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["toolbox"]


def test_detection_reads_copied_build(adapter, tmp_path):
    adapter.prepare_workspace(tmp_path, {}, toolbox_url=TOOLBOX)
    _write(tmp_path, {"url": "http://old-toolbox:3001/mcp"})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["toolbox"]


def test_direct_servers_match_written_config(adapter, tmp_path):
    adapter.prepare_workspace(tmp_path, {}, toolbox_url=TOOLBOX)
    adapter.inject_runtime_params(tmp_path, job_id=None, run_id="run-1", capability_token="tok",
                                  toolbox_url=TOOLBOX)
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == []
