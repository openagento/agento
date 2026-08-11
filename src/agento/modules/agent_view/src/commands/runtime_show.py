"""CLI command: agent_view:runtime — dump resolved runtime profile as JSON.

Used internally by `agento run <code>` to discover harness + HOME path before
spawning the sandbox, and available to users for introspection/debugging.

When ``--prompt`` is supplied, the payload also includes a ``headless_command``
built by the harness's registered :class:`CommandBuilder`. When no prompt is given,
only ``interactive_command`` is populated. Both fields are ``null`` if no
no harness is registered for the resolved harness id.
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
        return "Dump resolved runtime profile (workspace, harness, provider, HOME path, CLI command) as JSON"

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
            help="Show the interactive command in bypass mode (with the harness's skip-approvals flag)",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.agent_view_runtime import resolve_agent_view_runtime
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.db import get_connection_or_exit
        from agento.framework.harness import (
            HarnessRunContext,
            RunRequest,
            find_harness,
        )
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
        registered = find_harness(runtime.harness) if runtime.harness else None
        if registered is not None and runtime.provider:
            builder = registered.adapter.command_builder
            # Display only: no credential is claimed here, so credential_required is
            # False — the real claim happens in agent_view:prepare-run.
            ctx = HarnessRunContext(
                harness=runtime.harness,
                provider=runtime.provider,
                model=effective_model,
                home_dir=home,
                credential_required=False,
            )
            interactive_command = builder.interactive(ctx, yolo=getattr(args, "yolo", False))
            if args.prompt:
                headless_command = builder.headless(
                    ctx, RunRequest(prompt=args.prompt, model=effective_model)
                )

        payload = {
            "agent_view_id": av.id,
            "agent_view_code": av.code,
            "workspace_id": runtime.workspace.id,
            "workspace_code": runtime.workspace.code,
            "harness": runtime.harness,
            "provider": runtime.provider,
            "model": runtime.model,
            "home": home,
            "interactive_command": interactive_command,
            "headless_command": headless_command,
        }
        print(json.dumps(payload))
