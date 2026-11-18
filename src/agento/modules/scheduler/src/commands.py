"""`scheduler:sweep-uploads`: the hourly removal of staged schedule files that nothing names
(`store.sweep_uploads`)."""
from __future__ import annotations

import argparse

from agento.framework.cli.runtime import _load_framework_config
from agento.framework.db import get_connection_or_exit

from . import store


class SchedulerSweepUploadsCommand:
    @property
    def name(self) -> str:
        return "scheduler:sweep-uploads"

    @property
    def shortcut(self) -> str:
        return "sc:su"

    @property
    def help(self) -> str:
        return "Remove staged schedule files that no schedule and no open run names"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            removed = store.sweep_uploads(
                conn, retention_hours=int(store.setting(conn, "limits/upload_retention_hours")))
            print(f"Removed {removed} stale schedule upload(s).")
        finally:
            conn.close()
