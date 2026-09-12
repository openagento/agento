"""Shared plumbing for the artifact:* commands — one toolbox contract in one place.

Every one of these commands runs on the HOST (they are in `_LOCAL_MODULE_COMMANDS`),
resolves the compose file itself, and execs into the toolbox, which is the only
container that can reach the store. They therefore share one reply contract, and
duplicating it per command is how three commands end up with three different ideas
of what a malformed reply means.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

# docker-compose mounts the modules root at /app/modules/core, so each module is
# a child of it.
TOOLBOX_CLI = "/app/modules/core/versioned_artifacts/toolbox/cli.js"


def _last_json_object(stdout: str) -> dict:
    """The last line that decodes to a JSON OBJECT.

    `json.loads` happily returns `null`, `[]` or `3` — and calling `.get()` on any
    of them raises, which is how a well-formed but unexpected toolbox line turned
    into a Python traceback on an administrative command that promises never to
    show one.
    """
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


# The one shape the toolbox writes for an operator: the `log(tool, status, details)`
# line its CLI wrapper prints to stderr.
_TOOLBOX_LOG_LINE = re.compile(r"^\[[a-z0-9_]+\] (?:ERROR|WARN|OK)\b")


def _sanitized(stderr: str, fallback: str) -> str:
    """One short line, and only one the toolbox meant for an operator.

    An ALLOWLIST, not a blocklist. The toolbox is a different process on the other
    side of `docker compose exec`, and a Node stack has more shapes than can be
    enumerated — `at ...`, `file://...`, `node:internal/modules/esm/resolve:275`,
    a bare source line, a caret line — so every list of bad shapes lets the next
    one through (PRD section 47). The toolbox's own operator lines have exactly one
    form; anything else is the fixed fallback. The child also carries its own outer
    catch now, so a stack reaching here at all means something below our contract
    failed, and that is precisely when guessing is worst.
    """
    for line in stderr.strip().splitlines():
        line = line.strip()
        if _TOOLBOX_LOG_LINE.match(line):
            return line[:200]
    return fallback


def compose_flags() -> list[str]:
    """The compose flags every artifact command needs, or one line and exit 1."""
    from agento.framework.cli._project import compose_file_flags, find_project_root

    project_root = find_project_root()
    if not project_root:
        print("Error: Not inside an agento project. Run 'agento install' first.", file=sys.stderr)
        sys.exit(1)
    flags = compose_file_flags(project_root)
    if not flags:
        print("Error: docker-compose.yml not found.", file=sys.stderr)
        sys.exit(1)
    return flags


def run_toolbox(flags: list[str], argv: list[str], payload: dict) -> tuple[dict, subprocess.CompletedProcess]:
    """Exec the toolbox CLI with a JSON payload on stdin; return (body, result)."""
    result = subprocess.run(
        ["docker", "compose", *flags, "exec", "-T", "toolbox", "node", TOOLBOX_CLI, *argv],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    return _last_json_object(result.stdout), result


def fail_on_error(body: dict, result: subprocess.CompletedProcess, fallback: str) -> None:
    """The shared failure branch: an error_code or a non-zero exit ends the command."""
    if result.returncode != 0 or body.get("error_code"):
        message = body.get("message") or _sanitized(result.stderr, fallback)
        print(f"Error: {body.get('error_code', 'FAILED')}: {message}", file=sys.stderr)
        sys.exit(1)
