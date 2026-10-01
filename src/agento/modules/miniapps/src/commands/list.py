"""CLI command: miniapp:list — the activated miniapp versions."""
from __future__ import annotations

import argparse

from ._toolbox import call


class MiniappListCommand:
    @property
    def name(self) -> str:
        return "miniapp:list"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "List activated miniapp versions and their allowed actions"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        rows = call("list", args, {}, "could not list the miniapps").get("activations") or []
        if not rows:
            print("No activated miniapps.")
            return
        for r in rows:
            actions = ", ".join(r.get("allowed_actions") or []) or "(none)"
            print(f"{r.get('artifact_code')}  {r.get('version_id')}  actions: {actions}")
