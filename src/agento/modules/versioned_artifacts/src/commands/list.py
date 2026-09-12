"""CLI command: artifact:list — what the store holds, with its metadata and preview."""
from __future__ import annotations

import argparse
import sys

from ._toolbox import compose_flags, fail_on_error, run_toolbox


class VersionedArtifactListCommand:
    @property
    def name(self) -> str:
        return "artifact:list"

    @property
    def shortcut(self) -> str:
        # No alias: every name the CLI accepts for this command must also sit in
        # _LOCAL_MODULE_COMMANDS or it gets proxied into cron, which has neither
        # the docker socket nor the storage volume.
        return ""

    @property
    def help(self) -> str:
        return "List versioned artifacts (administrative; not an agent tool)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        flags = compose_flags()
        body, result = run_toolbox(flags, ["--op", "list", "--actor", args.actor], {})
        fail_on_error(body, result, "the artifact listing failed")
        # Absence of an error_code is not evidence of success: a reply without the
        # `artifacts` list would otherwise print an empty table that reads as
        # "this deployment has no artifacts".
        artifacts = body.get("artifacts")
        if not isinstance(artifacts, list):
            print("Error: FAILED: the toolbox did not return an artifact list", file=sys.stderr)
            raise SystemExit(1)
        if not artifacts:
            print("No artifacts.")
            return
        for a in artifacts:
            print(f"{a.get('artifact_code')}  {a.get('current_version')}  "
                  f"{a.get('title') or '-'}  {a.get('owner') or '-'}  "
                  f"{a.get('created_at') or '-'}  {a.get('preview_url') or '-'}")
