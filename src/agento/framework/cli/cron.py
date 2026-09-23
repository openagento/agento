"""``cron:run`` — the only entry point the root crontab uses for module cron jobs.

Root renders one crontab line per module cron job at container start. A module can
be disabled *after* that, so every line is wrapped in this dispatcher: it resolves the
module's enabled state at fire time and exits 0 without touching the module when it is
disabled. The inner command runs **in-process** — an ``exec`` here would discard the
credential store delivered on a file descriptor and reset ``PR_SET_DUMPABLE``.
"""
from __future__ import annotations

import argparse
import sys


class CronRunCommand:
    @property
    def name(self) -> str:
        return "cron:run"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Run a module's cron command (internal)"

    @property
    def hidden(self) -> bool:
        return True

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("module", help="Module the cron job belongs to")
        parser.add_argument(
            "inner", nargs=argparse.REMAINDER, help="Command and arguments to run",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from ..module_discovery import (
            iter_enabled_module_dirs,
            iter_module_dirs,
            resolve_module_root,
        )

        root = resolve_module_root()
        if args.module not in {d.name for d in iter_module_dirs(root)}:
            print(f"cron:run: unknown module '{args.module}'", file=sys.stderr)
            sys.exit(1)
        if args.module not in {d.name for d in iter_enabled_module_dirs(root)}:
            sys.exit(0)  # disabled since the crontab was rendered — nothing to do

        if not args.inner:
            print("cron:run: no command given", file=sys.stderr)
            sys.exit(1)

        from ..commands import get_commands, resolve_shortcut

        inner_name = resolve_shortcut(args.inner[0])
        command = get_commands().get(inner_name)
        if command is None:
            print(f"cron:run: unknown command '{args.inner[0]}'", file=sys.stderr)
            sys.exit(1)

        parser = argparse.ArgumentParser(prog=f"agento {inner_name}")
        command.configure(parser)
        command.execute(parser.parse_args(args.inner[1:]))
