"""CLI command: ``agent_view:prepare-run`` — cron-side prep for ``agento run``.

Composes the same pre-spawn pipeline the consumer uses (``TokenResolver`` +
``materialize_run_workspace``) and returns the result as JSON so the host
can ``docker exec`` into the sandbox with HOME/cwd/env already resolved.

The ``env`` field carries credentials only for providers whose ConfigWriter
chooses runtime env delivery. The host must inject these via name-only
``-e KEY`` (no ``=value``) so the secret never appears in ``ps``/``argv`` —
same stance as the recent stdin-only-secrets token:register hardening.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid


def _new_run_id() -> str:
    return f"run-{os.getpid()}-{uuid.uuid4().hex[:12]}"


class AgentViewPrepareRunCommand:
    @property
    def name(self) -> str:
        return "agent_view:prepare-run"

    @property
    def shortcut(self) -> str:
        return "av:pr"

    @property
    def help(self) -> str:
        return (
            "Prepare a run environment for an agent_view (token + artifacts + env) "
            "and dump JSON; used by `agento run` to exec into the sandbox."
        )

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("agent_view_code", help="Agent view code")
        parser.add_argument(
            "--prompt", default=None,
            help="Optional prompt for headless mode — if set, payload carries headless command.",
        )
        parser.add_argument(
            "--model", default=None,
            help="Optional model override (falls back to agent_view/model).",
        )
        parser.add_argument(
            "--yolo", action="store_true",
            help="Interactive bypass mode — build the interactive command with the "
                 "provider's skip-approvals flag (headless is always bypass).",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.agent_manager.models import AgentProvider
        from agento.framework.agent_manager.token_resolver import TokenResolver
        from agento.framework.agent_view_runtime import resolve_agent_view_runtime
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.cli_invoker import get_cli_invoker
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.config_writer import get_config_writer
        from agento.framework.db import get_connection_or_exit
        from agento.framework.run_preparation import materialize_run_workspace
        from agento.framework.scoped_config import Scope
        from agento.framework.workspace import get_agent_view_by_code

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            av = get_agent_view_by_code(conn, args.agent_view_code)
            if av is None:
                print(f"Error: agent_view '{args.agent_view_code}' not found", file=sys.stderr)
                sys.exit(1)

            runtime = resolve_agent_view_runtime(conn, av.id)
            if runtime.workspace is None:
                print(
                    f"Error: workspace for agent_view '{args.agent_view_code}' not found",
                    file=sys.stderr,
                )
                sys.exit(1)
            if runtime.provider is None:
                print(
                    "Error: agent_view/provider not configured. Set it via "
                    "`agento config:set agent_view/provider <claude|codex> "
                    f"--scope=agent_view --scope-id={av.id}`",
                    file=sys.stderr,
                )
                sys.exit(1)

            provider = AgentProvider(runtime.provider)

            # Resolve token from pool — stamps used_at, fails fast with actionable message.
            token = TokenResolver().resolve(conn, provider)

            # Shared toolbox URL + agent_view-scoped config for the materialize fallback
            # (mirrors consumer._run_job to keep the pipeline identical).
            core_cfg = ScopedConfigService(conn).get_module("core") or {}
            toolbox_url = core_cfg.get("toolbox/url") or "http://toolbox:3001"
            agent_config_svc = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id)
        finally:
            conn.close()

        home, working_dir = materialize_run_workspace(
            runtime,
            run_id=_new_run_id(),
            agent_config_svc=agent_config_svc,
            toolbox_url=toolbox_url,
            token=token,
        )

        writer = get_config_writer(provider)
        env = writer.credential_env(token)
        # Add GIT_AUTHOR_*/GIT_COMMITTER_* from the agent_view identity so the agent's commits are
        # authored correctly even in a clone with its own repo-local .git/config (env beats all
        # gitconfig levels). Non-secret, but delivered the same name-only -e way by `agento run`.
        from agento.framework.git_identity import (
            GIT_AUTHOR_EMAIL_PATH,
            GIT_AUTHOR_NAME_PATH,
            git_identity_env,
        )
        env = {
            **env,
            **git_identity_env(
                agent_config_svc.get(GIT_AUTHOR_NAME_PATH) or "",
                agent_config_svc.get(GIT_AUTHOR_EMAIL_PATH) or "",
            ),
        }

        effective_model = args.model or runtime.model
        # Mirror ``agent_view:runtime``: a missing CliInvoker yields a JSON
        # ``command: null`` so the host ``RunCommand`` can show its actionable
        # "no CliInvoker registered" hint instead of cron raising a traceback.
        command: list[str] | None
        try:
            invoker = get_cli_invoker(provider)
        except (ValueError, KeyError):
            command = None
        else:
            if args.prompt:
                command = invoker.headless_command(args.prompt, model=effective_model)
            else:
                command = invoker.interactive_command(yolo=getattr(args, "yolo", False))

        payload = {
            "agent_view_id": av.id,
            "agent_view_code": av.code,
            "workspace_id": runtime.workspace.id,
            "workspace_code": runtime.workspace.code,
            "provider": runtime.provider,
            "model": effective_model,
            "home": str(home) if home is not None else None,
            "working_dir": str(working_dir) if working_dir is not None else None,
            "command": command,
            "env": env,
            "token_id": token.id,
        }
        print(json.dumps(payload))
