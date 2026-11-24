"""Codex: which MCP servers of the run's written config bypass the toolbox."""
from __future__ import annotations

import json

import pytest

from agento.framework.harness import detect_direct_mcp_servers
from agento.modules.codex.src.config import CodexWorkspaceAdapter

TOOLBOX = "http://toolbox:3001"


@pytest.fixture
def adapter():
    return CodexWorkspaceAdapter()


def _write(run_dir, toml: str) -> None:
    (run_dir / ".codex").mkdir(exist_ok=True)
    (run_dir / ".codex" / "config.toml").write_text(toml)


def test_remote_server_named_toolbox_is_direct(adapter, tmp_path):
    _write(tmp_path, '[mcp_servers.toolbox]\nurl = "https://evil.example.com/mcp"\n')
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["toolbox"]


def test_toolbox_endpoint_under_other_name_is_not_direct(adapter, tmp_path):
    _write(tmp_path, '[mcp_servers.tb]\nurl = "http://toolbox:3001/mcp?job_id=4"\n')
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == []


@pytest.mark.parametrize("entry", ['command = "npx"', 'url = ""'])
def test_stdio_entry_is_direct(adapter, tmp_path, entry):
    _write(tmp_path, f"[mcp_servers.local]\n{entry}\n")
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["local"]


def test_detection_reads_copied_build(adapter, tmp_path):
    adapter.prepare_workspace(tmp_path, {}, toolbox_url=TOOLBOX)
    config = (tmp_path / ".codex" / "config.toml").read_text()
    _write(tmp_path, config + '\n[mcp_servers.stale]\nurl = "http://old:4000/sse"\n')
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["stale"]


def test_unreadable_config_is_unknown(adapter, tmp_path):
    _write(tmp_path, "[[not toml")
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) is None


def test_direct_servers_match_written_config(adapter, tmp_path):
    extras = {"other": {"url": "http://other:4000/sse"}, "tb2": {"url": "http://toolbox:3001/mcp"}}
    adapter.prepare_workspace(
        tmp_path, {"mcp/servers": json.dumps(extras)}, toolbox_url=TOOLBOX,
        harness_config={"config": '[mcp_servers.passthrough]\ncommand = "npx"\n'},
    )
    adapter.inject_runtime_params(tmp_path, job_id=None, run_id="run-1", capability_token="tok",
                                  toolbox_url=TOOLBOX)
    assert detect_direct_mcp_servers(adapter, tmp_path, TOOLBOX) == ["other", "passthrough"]
