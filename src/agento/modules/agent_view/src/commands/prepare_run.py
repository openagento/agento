"""CLI command: ``agent_view:prepare-run`` — cron-side prep for ``agento run``.

Composes the same pre-spawn pipeline the consumer uses (``CredentialResolver`` +
``materialize_run_workspace``) and returns the result as JSON so the host
can ``docker exec`` into the sandbox with HOME/cwd/env already resolved.

The ``env`` field carries credentials only for providers whose WorkspaceAdapter
chooses runtime env delivery. The host must inject these via name-only
``-e KEY`` (no ``=value``) so the secret never appears in ``ps``/``argv`` —
same stance as the recent stdin-only-secrets credential:register hardening.
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
            "Prepare a run environment for an agent_view (credential + artifacts + env) "
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
                 "harness's skip-approvals flag (headless is always bypass).",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.agent_manager.credential_resolver import CredentialResolver
        from agento.framework.agent_view_runtime import resolve_agent_view_runtime
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.config_resolver import ScopedConfigService
        from agento.framework.db import get_connection_or_exit
        from agento.framework.harness import (
            HarnessRunContext,
            RunRequest,
            StreamRenderer,
            get_harness,
            get_harness_config,
            resolve_provider,
            workspace_adapter_for,
        )
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
            if runtime.harness is None:
                print(
                    "Error: agent_view/harness not configured. Set it via "
                    "`agento config:set agent_view/harness <harness> "
                    f"--scope=agent_view --scope-id={av.id}`",
                    file=sys.stderr,
                )
                sys.exit(1)

            provider_desc = resolve_provider(runtime.harness, runtime.provider)

            # The CALLER claims the credential — once — and stamps used_at.
            # ``credential is None`` is legal in TWO cases: the provider requires none,
            # or this is the explicitly interactive path, where starting the CLI with no
            # credential is how an operator reaches `/login` to repair an empty pool.
            # Headless keeps failing fast — a prompt run with no credential would only
            # fail deeper in, after burning a session.
            credential = None
            # Set when the provider needs a credential but we could not claim one: the
            # interactive path continues so the operator can `/login`, and the copied
            # build's stale credential state must be removed rather than inherited.
            purge_credentials = False
            if provider_desc.credential_required:
                try:
                    credential = CredentialResolver().resolve(
                        conn, provider_desc.credential_scope
                    )
                except Exception as exc:
                    if args.prompt:
                        print(f"Error: {exc}", file=sys.stderr)
                        sys.exit(1)
                    purge_credentials = True
                    print(
                        f"Warning: no usable {provider_desc.credential_scope} credential "
                        f"({exc}). Starting the agent without one — use its own /login, "
                        f"or run `agento credential:register "
                        f"{provider_desc.credential_scope} <label>`.",
                        file=sys.stderr,
                    )

            # Shared toolbox URL + agent_view-scoped config for the materialize fallback
            # (mirrors consumer._run_job to keep the pipeline identical).
            core_cfg = ScopedConfigService(conn).get_module("core") or {}
            toolbox_url = core_cfg.get("toolbox/url") or "http://toolbox:3001"
            agent_config_svc = ScopedConfigService(conn, Scope.AGENT_VIEW, av.id)
        finally:
            conn.close()

        # Computed BEFORE materializing, not after: the workspace bakes the model
        # expectation the bridge enforces, so an `--model` override decided later cannot
        # reach it and the run is failed for doing exactly what was asked.
        effective_model = args.model or runtime.model

        home, working_dir = materialize_run_workspace(
            runtime,
            run_id=_new_run_id(),
            agent_config_svc=agent_config_svc,
            toolbox_url=toolbox_url,
            credential=credential,
            purge_credentials=purge_credentials,
            effective_model=effective_model,
        )

        writer = workspace_adapter_for(runtime.harness)
        env = writer.credential_env(credential) if credential is not None else {}
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

        # Mirror ``agent_view:runtime``: an unregistered harness yields a JSON
        # ``command: null`` so the host ``RunCommand`` can show its actionable
        # "no harness registered" hint instead of cron raising a traceback.
        command: list[str] | None
        # Dotted path of the harness's own stream renderer, for `agento run --pretty`.
        # ``getattr`` because the member is optional: a harness without one simply
        # has no pretty mode and the host streams raw.
        stream_renderer: str | None = None
        stdin_payload: str | None = None
        try:
            harness_entry = get_harness(runtime.harness)
            adapter = harness_entry.adapter
            builder = adapter.command_builder
        except (ValueError, KeyError):
            command = None
        else:
            renderer = getattr(adapter, "stream_renderer", None)
            # Ship the path only for something that actually satisfies the
            # protocol. A half-implemented renderer would otherwise reach the
            # host and raise once per event; degrading to raw is the safe answer.
            if isinstance(renderer, StreamRenderer):
                stream_renderer = (
                    f"{type(renderer).__module__}:{type(renderer).__qualname__}"
                )
            elif renderer is not None:
                print(
                    f"Warning: harness {runtime.harness!r} declares a stream_renderer "
                    f"that does not implement StreamRenderer.render — `agento run "
                    f"--pretty` will stream raw output.",
                    file=sys.stderr,
                )
            ctx = HarnessRunContext(
                harness=runtime.harness,
                provider=provider_desc.id,
                model=effective_model,
                working_dir=str(working_dir) if working_dir is not None else "/workspace",
                home_dir=str(home) if home is not None else None,
                credential_required=provider_desc.credential_required,
                credential=credential,
                harness_config=get_harness_config(agent_config_svc, harness_entry),
            )
            if args.prompt:
                req = RunRequest(prompt=args.prompt, model=effective_model)
                command = builder.headless(ctx, req)
                # A command is argv plus stdin — the host runner must deliver the same
                # stdin the consumer's runner would, or a stdin-only harness gets no prompt.
                stdin_payload = getattr(builder, "stdin_payload", lambda *_: None)(ctx, req)
            else:
                command = builder.interactive(ctx, yolo=getattr(args, "yolo", False))

        payload = {
            "agent_view_id": av.id,
            "agent_view_code": av.code,
            "workspace_id": runtime.workspace.id,
            "workspace_code": runtime.workspace.code,
            "harness": runtime.harness,
            "provider": runtime.provider,
            "model": effective_model,
            "home": str(home) if home is not None else None,
            "working_dir": str(working_dir) if working_dir is not None else None,
            "command": command,
            "stream_renderer": stream_renderer,
            "stdin": stdin_payload,
            "env": env,
            "credential_id": credential.id if credential is not None else None,
            # Deprecated duplicate of credential_id, kept for one release so an older
            # host-side `agento run` reading token_id keeps working.
            "token_id": credential.id if credential is not None else None,
        }
        print(json.dumps(payload))
