"""`agento run <agent_view_code> [--yolo] [--pretty] [prompt]` — spawn the configured agent CLI.

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
resolved provider, cron-side. ``--pretty`` follows the same rule: cron reports the
dotted path of the harness's own ``StreamRenderer`` and the host only imports it.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ..harness.stream_style import sanitize
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
            "--pretty",
            action="store_true",
            help="Render the agent's event stream as readable lines instead of raw "
                 "JSONL. Headless runs only (interactive is already readable).",
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
        # everything after the agent_view_code — including a ``--yolo``/``--pretty``
        # typed in the natural trailing position (`agento run dev --yolo`). Recover
        # them from the captured tokens so both orders work. Cost of the recovery:
        # a literal flag token inside the prompt text is stripped from the prompt.
        prompt_tokens = list(args.prompt)
        yolo = bool(getattr(args, "yolo", False)) or "--yolo" in prompt_tokens
        pretty = bool(getattr(args, "pretty", False)) or "--pretty" in prompt_tokens
        prompt_tokens = [t for t in prompt_tokens if t not in ("--yolo", "--pretty")]
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
            renderer = _load_stream_renderer(runtime) if pretty else None
            if renderer is not None:
                sys.exit(_run_pretty(exec_args, child_env, renderer))
            result = subprocess.run(
                exec_args, stdin=subprocess.DEVNULL, env=child_env,
            )
            sys.exit(result.returncode)

        if pretty:
            print(
                "Note: --pretty applies to headless runs (with a prompt); "
                "the interactive agent CLI already renders its own output.",
                file=sys.stderr,
            )

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


def _load_stream_renderer(runtime: dict):
    """Import the harness's own stream renderer from the path cron reported.

    ``prepare-run`` sends a dotted ``module:Class`` path taken from the harness
    adapter it already loaded — the host never maps a harness id to a module, so
    no harness literal appears here. Returns ``None`` whenever the renderer
    cannot be used, which makes ``--pretty`` degrade to the raw stream instead
    of failing the run.
    """
    path = runtime.get("stream_renderer")
    if not isinstance(path, str) or ":" not in path:
        return None
    module_name, _, class_name = path.partition(":")
    # The path comes from the container. Only the framework's own package is
    # importable here — a module loaded by file path cron-side gets a synthetic
    # name that this check rejects, along with anything else unexpected.
    if module_name != "agento" and not module_name.startswith("agento."):
        return None
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)()
    except Exception as exc:
        print(
            f"Note: --pretty unavailable ({exc}); streaming raw output.",
            file=sys.stderr,
        )
        return None


def _run_pretty(exec_args: list[str], child_env: dict, renderer) -> int:
    """Stream the agent's stdout through ``renderer``; return its exit code.

    A line the renderer cannot handle is printed raw, so no output is ever lost.
    ``stderr`` stays inherited, exactly as in the raw path. Every event is
    sanitized before rendering — see :func:`sanitize`; a raw passthrough line is
    still JSON-escaped and therefore inert.
    """
    proc = subprocess.Popen(
        exec_args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        env=child_env,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Covers a blank line too — the raw stream printed it, so this does.
            print(line, flush=True)
            continue
        if not isinstance(event, dict):
            print(line, flush=True)
            continue
        try:
            # Sanitize BEFORE rendering, so the renderer's own styling survives
            # and no renderer can forget to scrub its inputs.
            rendered = renderer.render(sanitize(event))
        except Exception:
            print(line, flush=True)
            continue
        # Only ``None`` suppresses. An empty string is a rendered result and is
        # printed as the blank line the renderer asked for.
        if rendered is not None:
            print(rendered, flush=True)
    return proc.wait()


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
