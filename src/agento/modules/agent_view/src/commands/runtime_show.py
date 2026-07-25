"""CLI command: agent_view:runtime — dump resolved runtime profile as JSON.

Used internally by `agento run <code>` to discover provider + HOME path before
spawning the sandbox, and available to users for introspection/debugging.

When ``--prompt`` is supplied, the payload also includes a ``headless_command``
built by the provider's registered :class:`CliInvoker`. When no prompt is given,
only ``interactive_command`` is populated. Both fields are ``null`` if no
CliInvoker is registered for the resolved provider.
"""
from __future__ import annotations

import argparse
import json
import sys


class AgentViewRuntimeCommand:
    @property
    def name(self) -> str:
        return "agent_view:runtime"

    @property
    def shortcut(self) -> str:
        return "av:ru"

    @property
    def help(self) -> str:
        return "Dump resolved runtime profile (workspace, provider, HOME path, CLI command) as JSON"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("agent_view_code", help="Agent view code")
        parser.add_argument(
            "--prompt",
            default=None,
            help="Optional prompt for headless mode — if set, response includes headless_command",
        )
        parser.add_argument(
            "--model",
            default=None,
            help="Optional model override (falls back to agent_view/model)",
        )
        parser.add_argument(
            "--yolo",
            action="store_true",
            help="Show the interactive command in bypass mode (with the provider's "
                 "skip-approvals flag)",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.agent_view_runtime import resolve_agent_view_runtime
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.cli_invoker import get_cli_invoker
        from agento.framework.db import get_connection_or_exit
        from agento.framework.workspace import get_agent_view_by_code

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            av = get_agent_view_by_code(conn, args.agent_view_code)
            if av is None:
                print(f"Error: agent_view '{args.agent_view_code}' not found", file=sys.stderr)
                sys.exit(1)

            runtime = resolve_agent_view_runtime(conn, av.id)
        finally:
            conn.close()

        if runtime.workspace is None:
            print(
                f"Error: workspace for agent_view '{args.agent_view_code}' not found",
                file=sys.stderr,
            )
            sys.exit(1)

        home = f"/workspace/build/{runtime.workspace.code}/{av.code}/current"
        interactive_command: list[str] | None = None
        headless_command: list[str] | None = None
        effective_model = args.model or runtime.model
        if runtime.provider:
            try:
                invoker = get_cli_invoker(runtime.provider)
            except (ValueError, KeyError):
                invoker = None
            if invoker is not None:
                interactive_command = invoker.interactive_command(
                    yolo=getattr(args, "yolo", False),
                )
                if args.prompt:
                    headless_command = invoker.headless_command(
                        args.prompt, model=effective_model,
                    )

        payload = {
            "agent_view_id": av.id,
            "agent_view_code": av.code,
            "workspace_id": runtime.workspace.id,
            "workspace_code": runtime.workspace.code,
            "provider": runtime.provider,
            "model": runtime.model,
            "home": home,
            "interactive_command": interactive_command,
            "headless_command": headless_command,
        }
        print(json.dumps(payload))
