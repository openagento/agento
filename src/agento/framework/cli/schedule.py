"""CLI command: ``scheduler:tick`` — fire due schedules, finalize deleted ones (framework cron.json)."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime

from ..db import get_connection_or_exit
from .runtime import _load_framework_config


class SchedulerTickCommand:
    @property
    def name(self) -> str:
        return "scheduler:tick"

    @property
    def shortcut(self) -> str:
        return "sc:ti"

    @property
    def help(self) -> str:
        return "Fire due schedules and finalize deleted ones (runs every minute from cron)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        from ..schedule.tick import TickConfig, tick

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            r = tick(conn, now=datetime.now(UTC), batch=TickConfig.from_env().batch)
        finally:
            conn.close()
        if r.lock_held:
            print("Another scheduler:tick is running; nothing done.")
            return
        print(f"Published {r.published}, skipped {r.skipped}, filled {r.filled}, disabled {r.disabled}, "
              f"finalized {r.finalized}, failed {r.failed}.")
