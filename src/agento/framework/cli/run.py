"""`agento run <agent_view_code> [--yolo] [prompt]` — spawn the configured agent CLI.

Host-side LOCAL command. Two-step docker exec:
1. Query cron for a full run profile via ``agent_view:prepare-run`` —
   resolves a token from the LRU pool, materializes the per-run artifacts
   dir (same path the consumer uses for jobs), and returns the unified CLI
   command + a possibly-non-empty ``env`` dict for credentials that must be
   delivered at runtime.
2. Exec into sandbox with HOME=per-run artifacts, cwd=per-run artifacts, running
   the command cron told us to run. Runtime env values are injected via
   **name-only** ``-e KEY`` so the secret never appears in argv/``ps``;
   docker reads the value from the parent's environment.

No harness literal appears in this file — the command string and env both
come from the registered harness's ``CommandBuilder``/``WorkspaceAdapter`` for the
resolved provider, cron-side.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ..ssh_prelude import wrap_with_ssh_prelude
from ._project import compose_file_flags, find_project_root


class RunCommand:
    @property
    def name(self) -> str:
        return "run"

    @property
    def shortcut(self) -> str:
        return "ru"

    @property
    def help(self) -> str:
        return (
            "Run the configured agent CLI in the sandbox container. "
            "With a prompt: headless (one-shot); without: interactive."
        )

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("agent_view_code", help="Agent view code (e.g. dev_01)")
        parser.add_argument(
            "--yolo",
            action="store_true",
            help="Interactive bypass mode — skip the agent's per-action approval prompts "
                 "(claude --dangerously-skip-permissions / codex "
                 "--dangerously-bypass-approvals-and-sandbox). Headless is always bypass.",
        )
        parser.add_argument(
            "prompt",
            nargs=argparse.REMAINDER,
            help="Optional one-shot prompt — when present, runs headless instead of interactive",
        )

    def execute(self, args: argparse.Namespace) -> None:
        project_root = find_project_root()
        if project_root is None:
            print(
                "Error: not inside an agento project. Run from the project root.",
                file=sys.stderr,
            )
            sys.exit(1)

        compose_flags = compose_file_flags(project_root)
        if not compose_flags:
            print("Error: docker-compose.yml not found.", file=sys.stderr)
            sys.exit(1)

        # ``prompt`` is captured via argparse.REMAINDER, which greedily swallows
        # everything after the agent_view_code — including a ``--yolo`` typed in the
        # natural trailing position (`agento run dev --yolo`). Recover it from the
        # captured tokens so both `--yolo dev` and `dev --yolo` work.
        prompt_tokens = list(args.prompt)
        yolo = bool(getattr(args, "yolo", False))
        if "--yolo" in prompt_tokens:
            yolo = True
            prompt_tokens = [t for t in prompt_tokens if t != "--yolo"]
        prompt = " ".join(prompt_tokens).strip()
        runtime = _fetch_runtime(
            compose_flags, args.agent_view_code, prompt=prompt, yolo=yolo,
        )
        harness = runtime.get("harness")
        if harness is None:
            print(
                f"Error: agent_view '{args.agent_view_code}' has no harness configured.\n"
                f"  Set it with:\n"
                f"    agento config:set agent_view/harness <harness> "
                f"--agent-view {args.agent_view_code}",
                file=sys.stderr,
            )
            sys.exit(1)

        command = runtime.get("command")
        if not command:
            print(
                f"Error: harness {harness!r} is not registered. "
                f"The agent module must declare it under 'agent_harnesses' in di.json.",
                file=sys.stderr,
            )
            sys.exit(1)

        home_in_container = runtime["home"]
        # working_dir is the per-run artifacts dir cron prepared (mirrors job
        # layout); fall back to HOME if cron didn't materialize one (blank job
        # path) so the exec still has a valid cwd.
        working_dir = runtime.get("working_dir") or home_in_container

        if not _host_build_exists(project_root, runtime):
            print(
                f"Error: no build found for agent_view '{args.agent_view_code}'.\n"
                f"  Build it with:\n"
                f"    agento workspace:build --agent-view {args.agent_view_code}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Runtime secret env (e.g. ANTHROPIC_API_KEY) delivery — name-only -e
        # so the value never lands in argv. Values come from the cron-side
        # WorkspaceAdapter.credential_env hook, identical to the consumer's path.
        secret_env: dict[str, str] = runtime.get("env") or {}
        env_args: list[str] = []
        for key in secret_env:
            env_args.extend(["-e", key])

        wrapped = wrap_with_ssh_prelude(list(command))

        if prompt:
            exec_args = [
                "docker", "compose", *compose_flags,
                "exec", "-T",
                "-u", "agent",
                "-e", f"HOME={home_in_container}",
                *env_args,
                "-w", working_dir,
                "sandbox",
                *wrapped,
            ]
            # Pass the secret via the child env so docker reads it from there
            # for each name-only -e KEY entry — never via argv.
            child_env = {**os.environ, **secret_env}
            result = subprocess.run(
                exec_args, stdin=subprocess.DEVNULL, env=child_env,
            )
            sys.exit(result.returncode)

        term = os.environ.get("TERM", "xterm-256color")
        exec_args = [
            "docker", "compose", *compose_flags,
            "exec", "-it",
            "-u", "agent",
            "-e", f"HOME={home_in_container}",
            "-e", f"TERM={term}",
            "-e", "COLORTERM=truecolor",
            *env_args,
            "-w", working_dir,
            "sandbox",
            *wrapped,
        ]
        # execvp inherits the current process's environment; set secrets there
        # so docker resolves them from the parent for each name-only -e KEY.
        os.environ.update(secret_env)
        os.execvp("docker", exec_args)


def _fetch_runtime(
    compose_flags: list[str], agent_view_code: str, *, prompt: str = "",
    yolo: bool = False,
) -> dict:
    """Ask cron to prepare a run environment; return parsed JSON.

    Calls ``agent_view:prepare-run`` so the host gets the same token-pool +
    materialization the consumer's jobs use — including the resolved ``env``
    dict for credentials that require runtime env delivery. Credentials
    materialized into the per-run HOME yield ``env={}``.
    """
    cmd = [
        "docker", "compose", *compose_flags,
        "exec", "-T", "cron",
        "/opt/cron-agent/run.sh", "agent_view:prepare-run", agent_view_code,
    ]
    if prompt:
        cmd.extend(["--prompt", prompt])
    if yolo:
        cmd.append("--yolo")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        # NEVER echo result.stdout — prepare-run's JSON carries API-key values
        # in the ``env`` field. If parsing fails (e.g. a stray warning sneaked
        # in before the JSON), echoing stdout would leak the secret to stderr.
        print(
            f"Error: could not parse runtime JSON from cron: {exc}\n"
            f"  (stdout suppressed to avoid leaking credentials carried in the env field)",
            file=sys.stderr,
        )
        sys.exit(1)


def _host_build_exists(project_root: Path, runtime: dict) -> bool:
    """Check that workspace/build/<ws>/<av>/current exists on the host."""
    ws = runtime.get("workspace_code")
    av = runtime.get("agent_view_code")
    if not ws or not av:
        return False
    current = project_root / "workspace" / "build" / ws / av / "current"
    return current.is_symlink() or current.exists()
