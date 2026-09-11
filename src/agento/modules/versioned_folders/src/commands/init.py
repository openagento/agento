"""CLI command: versioned-folder:init — create a versioned folder from a source directory."""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

# The pre-flight exists so a huge source fails before it is base64-encoded into a
# pipe, but its numbers must be the CONFIGURED ones: hardcoding them here would
# make a `config:set versioned_folders/limits/...` silently ineffective on the one
# path that reads a host directory. The toolbox resolves them through the normal
# ENV -> DB -> config.json fallback and prints them on request.
LIMIT_KEYS = ("max_file_size", "max_files", "max_total_size")

# The success shape, asserted rather than assumed. `_resolve_limits` already
# validates the toolbox's limits reply field by field; the success reply was
# trusted instead, so exit 0 plus `{}` printed "Created folder 'None' at version
# None" — the command reported a folder that does not exist. An administrative
# command that promises never to show a traceback must also never report a
# success it cannot see, so the version id is matched against the same pattern
# the tools enforce.
# `fullmatch`, not `match`: Python's `$` also matches the position BEFORE a final
# newline, so `match` accepted "v-20260905-154012-a3f2\n" — an invalid reply reported
# as success, with its extra line in the terminal. The anchors are kept so the pattern
# still reads as a whole-string rule wherever it is quoted.
VERSION_ID_RE = re.compile(r"^v-\d{8}-\d{6}-[a-z0-9]{4}$")

# docker-compose mounts the modules root at /app/modules/core, so each module is
# a child of it.
TOOLBOX_CLI = "/app/modules/core/versioned_folders/toolbox/cli.js"


def collect_source(
    root: Path,
    max_file_size: int,
    max_files: int,
    max_total_size: int,
) -> list[dict]:
    """Walk `root` into the init payload: [{path, content(base64), encoding}].

    A symlink raises rather than being skipped: an administrator must learn their
    source contained one instead of getting a folder quietly missing files.
    """
    root = Path(root)
    files: list[dict] = []
    total = 0
    for entry in sorted(root.rglob("*")):
        rel = entry.relative_to(root)
        if rel.parts and rel.parts[0] == ".git":
            continue
        if entry.is_symlink():
            raise ValueError(f"SYMLINK_NOT_ALLOWED: {rel} is a symbolic link")
        if not entry.is_file():
            continue
        size = entry.stat().st_size
        if size > max_file_size:
            raise ValueError(f"FILE_TOO_LARGE: {rel} is {size} bytes (limit {max_file_size})")
        total += size
        if total > max_total_size:
            raise ValueError(f"FOLDER_TOO_LARGE: source exceeds {max_total_size} bytes")
        files.append(
            {
                "path": rel.as_posix(),
                "content": base64.b64encode(entry.read_bytes()).decode("ascii"),
                "encoding": "base64",
            }
        )
        if len(files) > max_files:
            raise ValueError(f"TOO_MANY_FILES: source exceeds {max_files} files")
    return files


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


class VersionedFolderInitCommand:
    @property
    def name(self) -> str:
        return "versioned-folder:init"

    @property
    def shortcut(self) -> str:
        return "vf:init"

    @property
    def help(self) -> str:
        return "Create a versioned folder (administrative; not an agent tool)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("folder_code", help="Folder code, e.g. openagento-website")
        parser.add_argument("--source", help="Directory to import as the first version")
        parser.add_argument("--actor", default="admin", help="Who is creating this folder")

    @staticmethod
    def _resolve_limits(flags: list[str]) -> dict[str, int]:
        """Ask the toolbox for the effective limits; it owns the config fallback."""
        result = subprocess.run(
            ["docker", "compose", *flags, "exec", "-T", "toolbox",
             "node", TOOLBOX_CLI, "--print-limits"],
            input="{}", capture_output=True, text=True,
        )
        limits = _last_json_object(result.stdout)
        missing = [k for k in LIMIT_KEYS if not isinstance(limits.get(k), int) or limits[k] <= 0]
        if result.returncode != 0 or missing:
            detail = _sanitized(result.stderr, "no limits returned")
            print(f"Error: could not read the configured limits from the toolbox: {detail}", file=sys.stderr)
            sys.exit(1)
        return limits

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.cli._project import compose_file_flags, find_project_root

        files: list[dict] = []
        source_dir: Path | None = None
        if args.source:
            source = Path(args.source).expanduser().resolve()
            if not source.is_dir():
                print(f"Error: --source '{args.source}' is not a directory.", file=sys.stderr)
                sys.exit(1)
            source_dir = source

        project_root = find_project_root()
        if not project_root:
            print("Error: Not inside an agento project. Run 'agento install' first.", file=sys.stderr)
            sys.exit(1)
        flags = compose_file_flags(project_root)
        if not flags:
            print("Error: docker-compose.yml not found.", file=sys.stderr)
            sys.exit(1)

        if source_dir:
            limits = self._resolve_limits(flags)
            try:
                files = collect_source(
                    source_dir,
                    limits["max_file_size"],
                    limits["max_files"],
                    limits["max_total_size"],
                )
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)

        payload = json.dumps({"folder_code": args.folder_code, "files": files})
        result = subprocess.run(
            [
                "docker", "compose", *flags,
                "exec", "-T", "toolbox",
                "node", TOOLBOX_CLI, "--actor", args.actor,
            ],
            input=payload,
            capture_output=True,
            text=True,
        )
        body = _last_json_object(result.stdout)
        if result.returncode != 0 or body.get("error_code"):
            message = body.get("message") or _sanitized(result.stderr, "folder creation failed")
            print(f"Error: {body.get('error_code', 'FAILED')}: {message}", file=sys.stderr)
            sys.exit(1)
        # Absence of an error_code is not evidence of success. An empty, scalar,
        # malformed or partial reply lands here with exit 0, and printing what it
        # does not contain reports a folder nobody created.
        version = body.get("current_version")
        if body.get("folder_code") != args.folder_code or not (
            isinstance(version, str) and VERSION_ID_RE.fullmatch(version)
        ):
            detail = _sanitized(result.stderr, "no folder was reported")
            print(f"Error: FAILED: the toolbox did not confirm the folder: {detail}", file=sys.stderr)
            sys.exit(1)
        print(f"Created folder '{body.get('folder_code')}' at version {version}")
