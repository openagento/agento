"""CLI command: tool:sensitive -- mark a tool as sensitive for dry runs in scoped config."""
from __future__ import annotations

import argparse
import re

from agento.framework.scoped_config import Scope


class ToolSensitiveCommand:
    @property
    def name(self) -> str:
        return "tool:sensitive"

    @property
    def shortcut(self) -> str:
        return "tl:se"

    @property
    def help(self) -> str:
        return "Mark a tool as sensitive: a dry run gets a stub answer (set tools/{name}/sensitive = 1)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("tool_name", help="Name of the tool")
        parser.add_argument("--off", action="store_true", help="Set it to 0: a dry run calls the tool")
        parser.add_argument("--agent-view", dest="agent_view_code", default=None, help="Agent view code (shortcut for --scope agent_view)")
        parser.add_argument("--scope", default=Scope.DEFAULT, choices=[Scope.DEFAULT, Scope.WORKSPACE, Scope.AGENT_VIEW], help="Config scope (default: default)")
        parser.add_argument("--scope-id", dest="scope_id", type=int, default=0, help="Scope ID (default: 0)")

    def execute(self, args: argparse.Namespace) -> None:
        _validate_tool_name(args.tool_name)

        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.db import get_connection
        from agento.framework.scoped_config import scoped_config_set

        value = "0" if args.off else "1"
        db_config, _, _ = _load_framework_config()
        conn = get_connection(db_config)
        try:
            scope, scope_id = _resolve_scope(conn, args)
            path = f"tools/{args.tool_name}/sensitive"
            scoped_config_set(conn, path, value, scope=scope, scope_id=scope_id)
            conn.commit()
            print(f"Set tools/{args.tool_name}/sensitive = {value} (scope={scope}, scope_id={scope_id})")
        finally:
            conn.close()


_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_tool_name(name: str) -> None:
    if not _TOOL_NAME_RE.match(name):
        raise SystemExit(
            f"Invalid tool name '{name}'. Must be snake_case (lowercase letters, digits, underscores)."
        )


def _resolve_scope(conn, args):
    """Resolve scope from --agent-view shortcut or explicit --scope/--scope-id."""
    if args.agent_view_code:
        from agento.framework.workspace import get_agent_view_by_code
        av = get_agent_view_by_code(conn, args.agent_view_code)
        if av is None:
            raise SystemExit(f"Error: agent_view '{args.agent_view_code}' not found")
        return Scope.AGENT_VIEW, av.id
    return args.scope, args.scope_id
