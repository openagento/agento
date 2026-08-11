"""``serialize_toolbox_connection`` must not assume MCP.

Pi has no MCP at all ("build CLI tools with READMEs, or an extension that adds MCP
support"), so the framework hands over a plain :class:`ToolboxConnectionSpec` and every
harness materializes it its own way. The framework's own call path still routes Toolbox
wiring through ``prepare_workspace`` (the transport-agnostic rewrite is Etap 2), so these
tests exist to keep the declared seam implemented and honest on every shipped adapter
rather than letting it rot as unexercised surface.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.harness import (
    ToolboxConnectionSpec,
    list_harnesses,
    parse_harness_declarations,
    register_harness,
)
from agento.framework.module_loader import import_class

pytestmark = pytest.mark.usefixtures("builtin_harnesses")

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"


def _spec() -> ToolboxConnectionSpec:
    return ToolboxConnectionSpec(
        name="toolbox",
        transport="http",
        url="http://toolbox:3001/mcp?agent_view_id=2",
        headers={"Authorization": "Bearer sk-SECRET-TOOLBOX"},
    )


class TestEveryShippedAdapterImplementsIt:
    def test_all_registered_adapters_materialize_something(self, tmp_path):
        for registered in list_harnesses():
            # Callers always hand over an existing build/run dir (that is how
            # prepare_workspace is invoked), so the adapter is not required to mkdir.
            target = tmp_path / str(registered.descriptor.id)
            target.mkdir()
            registered.adapter.workspace_adapter.serialize_toolbox_connection(
                _spec(), target,
            )
            assert any(target.rglob("*")), f"{registered.descriptor.id} wrote nothing"

    def test_claude_writes_mcp_json(self, tmp_path):
        from agento.modules.claude.src.config import ClaudeWorkspaceAdapter

        ClaudeWorkspaceAdapter().serialize_toolbox_connection(_spec(), tmp_path)

        entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["toolbox"]
        assert entry["url"].endswith("agent_view_id=2")

    def test_codex_writes_its_own_toml(self, tmp_path):
        from agento.modules.codex.src.config import CodexWorkspaceAdapter

        CodexWorkspaceAdapter().serialize_toolbox_connection(_spec(), tmp_path)

        assert (tmp_path / ".codex" / "config.toml").exists()


class TestTransportAgnostic:
    def test_a_harness_may_materialize_it_as_anything(self, tmp_path):
        """The fake harness writes a flat text file — no MCP JSON anywhere. If the
        framework ever grew an assumption about the format, this fails."""
        module_dir = FIXTURES / "fake_harness"
        for decl in parse_harness_declarations(module_dir / "di.json", "fake_harness"):
            register_harness(decl.descriptor, import_class(module_dir, decl.class_path)())

        from agento.framework.harness import workspace_adapter_for

        workspace_adapter_for("fake").serialize_toolbox_connection(_spec(), tmp_path)

        assert (tmp_path / "fake-toolbox.txt").read_text() == (
            "toolbox http http://toolbox:3001/mcp?agent_view_id=2"
        )
        assert not (tmp_path / ".mcp.json").exists()


class TestSpecDoesNotLeakHeaders:
    def test_headers_are_suppressed_from_repr(self):
        """The spec can carry an auth header, and it gets logged like any dataclass."""
        assert "sk-SECRET-TOOLBOX" not in repr(_spec())

    def test_headers_are_still_readable_by_an_adapter(self):
        assert _spec().headers == {"Authorization": "Bearer sk-SECRET-TOOLBOX"}
