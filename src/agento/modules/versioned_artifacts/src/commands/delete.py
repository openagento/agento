"""CLI command: artifact:delete — remove an artifact from every place it lives."""
from __future__ import annotations

import argparse
import sys

from agento.framework.cli import terminal

from ._toolbox import compose_flags, fail_on_error, run_toolbox


class VersionedArtifactDeleteCommand:
    @property
    def name(self) -> str:
        return "artifact:delete"

    @property
    def shortcut(self) -> str:
        # No alias: see artifact:list.
        return ""

    @property
    def help(self) -> str:
        return "Delete an artifact, its versions and its served pages (no tool equivalent)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code, e.g. openagento-website")
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        # Before the prompt: outside a project there is nothing to delete, and asking
        # first would be a confirmation for a command that cannot run.
        flags = compose_flags()
        # The version count would cost a second round trip into the toolbox. The
        # question names what goes instead, which is what an operator has to weigh.
        choice = terminal.select(
            f"Delete artifact '{args.artifact_code}'? "
            f"Every version, every open draft and the served pages go, and this cannot be undone.",
            ["Keep it", f"Delete '{args.artifact_code}'"])
        if choice != 1:
            print("Cancelled. Nothing was removed.")
            return
        body, result = run_toolbox(
            flags, ["--op", "remove", "--actor", args.actor],
            {"artifact_code": args.artifact_code})
        fail_on_error(body, result, "the delete failed")
        if not body.get("removed_store") and not body.get("removed_published"):
            print("Error: FAILED: the toolbox did not confirm the delete", file=sys.stderr)
            raise SystemExit(1)
        print(f"Deleted '{args.artifact_code}'")
        # Named separately so a repair of a half-removed artifact reports what it found.
        print(f"  store:     {'removed' if body.get('removed_store') else 'was already gone'}")
        print(f"  published: {'removed' if body.get('removed_published') else 'was already gone'}")
