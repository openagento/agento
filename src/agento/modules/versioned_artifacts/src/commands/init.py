"""CLI command: artifact:init — create a versioned artifact from a source directory."""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

# Re-exported by import, not copied: three commands with three copies of the reply
# contract is three chances for them to disagree about what a malformed reply means.
from ._toolbox import _sanitized, compose_flags, fail_on_error, run_toolbox

# The pre-flight exists so a huge source fails before it is base64-encoded into a
# pipe, but its numbers must be the CONFIGURED ones: hardcoding them here would
# make a `config:set versioned_artifacts/limits/...` silently ineffective on the one
# path that reads a host directory. The toolbox resolves them through the normal
# ENV -> DB -> config.json fallback and prints them on request.
LIMIT_KEYS = ("max_file_size", "max_files", "max_total_size")

# The success shape, asserted rather than assumed. `_resolve_limits` already
# validates the toolbox's limits reply field by field; the success reply was
# trusted instead, so exit 0 plus `{}` printed "Created artifact 'None' at version
# None" — the command reported an artifact that does not exist. An administrative
# command that promises never to show a traceback must also never report a
# success it cannot see, so the version id is matched against the same pattern
# the tools enforce.
# `fullmatch`, not `match`: Python's `$` also matches the position BEFORE a final
# newline, so `match` accepted "v-20260905-154012-a3f2\n" — an invalid reply reported
# as success, with its extra line in the terminal. The anchors are kept so the pattern
# still reads as a whole-string rule wherever it is quoted.
VERSION_ID_RE = re.compile(r"^v-\d{8}-\d{6}-[a-z0-9]{4}$")


def collect_source(
    root: Path,
    max_file_size: int,
    max_files: int,
    max_total_size: int,
) -> list[dict]:
    """Walk `root` into the init payload: [{path, content(base64), encoding}].

    A symlink raises rather than being skipped: an administrator must learn their
    source contained one instead of getting an artifact quietly missing files.
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
            raise ValueError(f"ARTIFACT_TOO_LARGE: source exceeds {max_total_size} bytes")
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


class VersionedArtifactInitCommand:
    @property
    def name(self) -> str:
        return "artifact:init"

    @property
    def shortcut(self) -> str:
        # No alias: every name the CLI accepts for this command must also sit in
        # _LOCAL_MODULE_COMMANDS or it gets proxied into cron, which has neither
        # the docker socket nor the compose file.
        return ""

    @property
    def help(self) -> str:
        return "Create a versioned artifact from a host directory (operator equivalent of versioned_artifact_init)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code, e.g. openagento-website")
        parser.add_argument("--source", help="Directory to import as the first version")
        parser.add_argument("--actor", default="admin", help="Who is creating this artifact")
        parser.add_argument("--title", default=None, help="Human-readable title, stored as metadata")
        parser.add_argument("--owner", default=None, help="Who owns this artifact, stored as metadata")

    @staticmethod
    def _resolve_limits(flags: list[str]) -> dict[str, int]:
        """Ask the toolbox for the effective limits; it owns the config fallback."""
        limits, result = run_toolbox(flags, ["--print-limits"], {})
        missing = [k for k in LIMIT_KEYS if not isinstance(limits.get(k), int) or limits[k] <= 0]
        if result.returncode != 0 or missing:
            detail = _sanitized(result.stderr, "no limits returned")
            print(f"Error: could not read the configured limits from the toolbox: {detail}", file=sys.stderr)
            sys.exit(1)
        return limits

    def execute(self, args: argparse.Namespace) -> None:
        files: list[dict] = []
        source_dir: Path | None = None
        if args.source:
            source = Path(args.source).expanduser().resolve()
            if not source.is_dir():
                print(f"Error: --source '{args.source}' is not a directory.", file=sys.stderr)
                sys.exit(1)
            source_dir = source

        flags = compose_flags()

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

        body, result = run_toolbox(
            flags, ["--op", "init", "--actor", args.actor],
            {"artifact_code": args.artifact_code, "files": files,
             "title": args.title, "owner": args.owner})
        fail_on_error(body, result, "artifact creation failed")
        # Absence of an error_code is not evidence of success. An empty, scalar,
        # malformed or partial reply lands here with exit 0, and printing what it
        # does not contain reports an artifact nobody created.
        version = body.get("current_version")
        if body.get("artifact_code") != args.artifact_code or not (
            isinstance(version, str) and VERSION_ID_RE.fullmatch(version)
        ):
            detail = _sanitized(result.stderr, "no artifact was reported")
            print(f"Error: FAILED: the toolbox did not confirm the artifact: {detail}", file=sys.stderr)
            sys.exit(1)
        print(f"Created artifact '{body.get('artifact_code')}' at version {version}")
