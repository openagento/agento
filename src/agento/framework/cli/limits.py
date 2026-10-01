"""CLI commands: ``limits:prune`` and ``outbox:prune`` — the framework's two sweeps.

The limiter itself never deletes: a request path that also sweeps pays for a flood twice.
The sweep is a cron job, and `core/limits/bucket_retention_seconds` is the floor on how
long a bucket outlives its window, not a cap on a live one.
"""
from __future__ import annotations

import argparse

from ..db import get_connection_or_exit
from .runtime import _load_framework_config


class LimitsPruneCommand:
    @property
    def name(self) -> str:
        return "limits:prune"

    @property
    def shortcut(self) -> str:
        return "li:pr"

    @property
    def help(self) -> str:
        return "Delete expired rate-limit buckets"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        from agento.web.rate_limit import prune

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            print(f"Pruned {prune(conn)} expired limit bucket(s).")
        finally:
            conn.close()


class OutboxPruneCommand:
    @property
    def name(self) -> str:
        return "outbox:prune"

    @property
    def shortcut(self) -> str:
        return "ou:pr"

    @property
    def help(self) -> str:
        return "Delete job-event outbox rows and closed defer stretches past core/outbox/retention_days"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        # Both framework tables this epic adds, in one tick: a second cron entry for the
        # second table is a second thing to forget.
        from ..defer import prune_defer_stretches
        from ..outbox import prune_outbox

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            print(f"Pruned {prune_outbox(conn)} outbox row(s) "
                  f"and {prune_defer_stretches(conn)} closed defer stretch(es).")
        finally:
            conn.close()
