"""The framework's fail-safe call of the optional `WorkspaceAdapter.direct_mcp_servers`."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agento.framework.harness import detect_direct_mcp_servers


class _Without:
    pass


class _Raising:
    def direct_mcp_servers(self, artifacts_dir, toolbox_target):
        raise OSError("unreadable")


@pytest.mark.parametrize("adapter", [_Without(), _Raising(), MagicMock()], ids=["absent", "raises", "not-a-list"])
def test_an_adapter_that_cannot_report_is_unknown(adapter, tmp_path):
    assert detect_direct_mcp_servers(adapter, tmp_path, "http://toolbox:3001") is None


def test_the_adapter_gets_the_toolbox_origin(tmp_path):
    adapter = MagicMock()
    adapter.direct_mcp_servers.return_value = ["x"]
    assert detect_direct_mcp_servers(adapter, tmp_path, "http://toolbox:3001") == ["x"]
    assert adapter.direct_mcp_servers.call_args.args == (tmp_path, ("http", "toolbox", 3001))
