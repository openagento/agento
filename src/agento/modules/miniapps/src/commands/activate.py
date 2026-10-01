"""CLI command: miniapp:activate — let one artifact version run as a miniapp."""
from __future__ import annotations

import argparse

from ._toolbox import call


class MiniappActivateCommand:
    @property
    def name(self) -> str:
        return "miniapp:activate"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Activate an artifact version as a miniapp, with the actions its launches may call"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code")
        parser.add_argument("version_id", help="Version to activate, e.g. v-20260101-120000-abcd")
        parser.add_argument("--actions", default=None,
                            help="Comma-separated actions to allow (default: every action in miniapp.json)")
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        actions = [a.strip() for a in args.actions.split(",") if a.strip()] if args.actions is not None else None
        body = call("activate", args, {"artifact_code": args.artifact_code, "version_id": args.version_id,
                                       "actions": actions}, "could not activate the miniapp")
        allowed = ", ".join(body.get("allowed_actions") or []) or "(none)"
        print(f"Miniapp '{args.artifact_code}' {args.version_id} activated. Actions: {allowed}")
