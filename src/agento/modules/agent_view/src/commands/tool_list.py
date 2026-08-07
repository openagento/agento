"""CLI command: tool:list -- list all registered tools and their enabled status."""
from __future__ import annotations

import argparse


class ToolListCommand:
    @property
    def name(self) -> str:
        return "tool:list"

    @property
    def shortcut(self) -> str:
        return "tl:li"

    @property
    def help(self) -> str:
        return "List all registered tools and their enabled/disabled status"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--agent-view",
            dest="agent_view_code",
            help="Agent view code to check scoped config for",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.bootstrap import get_manifests
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.db import get_connection
        from agento.framework.scoped_config import Scope
        from agento.framework.workspace import get_agent_view_by_code

        db_config, _, _ = _load_framework_config()
        conn = get_connection(db_config)
        try:
            scope, scope_id, workspace_id = Scope.DEFAULT, 0, None

            if args.agent_view_code:
                agent_view = get_agent_view_by_code(conn, args.agent_view_code)
                if agent_view is None:
                    print(f"Error: agent_view '{args.agent_view_code}' not found")
                    return
                scope, scope_id, workspace_id = Scope.AGENT_VIEW, agent_view.id, agent_view.workspace_id

            # Resolve through the single config service (ENV -> DB -> config.json).
            # Tool names are snake_case, so .get() is dash-safe and reflects any
            # config.json first-class default.
            svc = ScopedConfigService(conn, scope, scope_id, workspace_id=workspace_id)

            # The status shown is the EFFECTIVE one: a tool whose declared `requires`
            # master is off is unavailable at runtime even when its own key is '1', so
            # printing "enabled (blocked by jira)" would be self-contradictory.
            from agento.framework.tool_enablement import blocked_by, is_effective, scan_tool_requires

            requires = scan_tool_requires()

            def resolve(tool_name: str) -> bool:
                return svc.get(f"tools/{tool_name}/is_enabled") == "1"

            manifests = get_manifests()
            tools = []
            for manifest in manifests:
                for tool in manifest.tools:
                    tool_name = tool["name"]
                    tools.append((
                        tool_name, manifest.name,
                        is_effective(tool_name, requires, resolve),
                        blocked_by(tool_name, requires, resolve),
                    ))

            if not tools:
                print("No tools registered.")
                return

            for tool_name, module_name, effective, blocker in tools:
                status = "enabled" if effective else "disabled"
                if blocker:
                    status += f" (blocked by {blocker})"
                print(f"  {tool_name:<30} {module_name:<20} {status}")
        finally:
            conn.close()
