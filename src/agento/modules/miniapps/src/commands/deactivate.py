"""CLI command: miniapp:deactivate — stop one artifact version running as a miniapp."""
from __future__ import annotations

import argparse

from ._toolbox import call


class MiniappDeactivateCommand:
    @property
    def name(self) -> str:
        return "miniapp:deactivate"

    @property
    def shortcut(self) -> str:
        return "mi:de"

    @property
    def help(self) -> str:
        return "Deactivate a miniapp version; its launches lose their actions at once"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code")
        parser.add_argument("version_id", help="Version to deactivate")
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        call("deactivate", args, {"artifact_code": args.artifact_code, "version_id": args.version_id},
             "could not deactivate the miniapp")
        print(f"Miniapp '{args.artifact_code}' {args.version_id} deactivated.")
