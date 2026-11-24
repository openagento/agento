"""Claude: which MCP servers of the run's written config bypass the toolbox."""
from __future__ import annotations

import json

import pytest

from agento.framework.harness import detect_direct_mcp_servers
from agento.modules.claude.src.config import ClaudeWorkspaceAdapter

TOOLBOX = "http://toolbox:3001"


@pytest.fixture
def adapter():
    return ClaudeWorkspaceAdapter()


def _write(run_dir, servers: dict) -> None:
    (run_dir / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))


def test_remote_server_named_toolbox_is_direct(adapter, tmp_path):
    _write(tmp_path, {"toolbox": {"type": "http", "url": "https://evil.example.com/mcp"}})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["toolbox"]


def test_toolbox_endpoint_under_other_name_is_not_direct(adapter, tmp_path):
    _write(tmp_path, {"tb": {"type": "http", "url": "http://toolbox:3001/mcp?job_id=4"}})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == []


@pytest.mark.parametrize("entry", [{"type": "stdio", "command": "npx"}, {"type": "http"}, "x"])
def test_stdio_entry_is_direct(adapter, tmp_path, entry):
    _write(tmp_path, {"local": entry})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["local"]


def test_user_and_project_scope_servers_are_direct(adapter, tmp_path):
    _write(tmp_path, {})
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"user": {"command": "x"}},
        "projects": {str(tmp_path): {"mcpServers": {"local": {"url": "http://other:1/mcp"}}}},
    }))
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["local", "user"]


def test_detection_reads_copied_build(adapter, tmp_path):
    """The run reads the copied build, which a stale build can make differ from live config."""
    adapter.prepare_workspace(tmp_path, {}, toolbox_url=TOOLBOX)
    _write(tmp_path, {**json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"],
                      "stale": {"type": "sse", "url": "http://old:4000/sse"}})
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["stale"]


def test_unreadable_config_is_unknown(adapter, tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) is None


def test_direct_servers_match_written_config(adapter, tmp_path):
    extras = {"other": {"url": "http://other:4000/sse"}, "local": {"command": "npx"},
              "tb2": {"url": "http://toolbox:3001/mcp"}}
    adapter.prepare_workspace(tmp_path, {"mcp/servers": json.dumps(extras)}, toolbox_url=TOOLBOX)
    adapter.inject_runtime_params(tmp_path, job_id=None, run_id="run-1", capability_token="tok",
                                  toolbox_url=TOOLBOX)
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["local", "other"]
