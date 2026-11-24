"""CLI command: ``tool:sync-catalog`` — sync the tool catalog from the enabled module manifests."""
from __future__ import annotations

import argparse

from ..db import get_connection_or_exit
from .runtime import _load_framework_config


class ToolSyncCatalogCommand:
    @property
    def name(self) -> str:
        return "tool:sync-catalog"

    @property
    def shortcut(self) -> str:
        return "tl:sc"

    @property
    def help(self) -> str:
        return "Sync the tool catalog from the enabled module manifests"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        from ..tool_catalog import sync_enabled_modules

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            result = sync_enabled_modules(conn)
        finally:
            conn.close()
        print(f"Tool catalog: {result.inserted} new, {result.changed} changed, {result.absent} absent.")
