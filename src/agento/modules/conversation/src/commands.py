"""`conversation:sweep` — the one cron tick that keeps a thread from stalling.

Two halves, in order:

* `sweep_pending` finishes a submission whose publish never happened (the §4.1 crash
  window), which is bounded by `sweep/pending_grace_seconds` so a live submission still in
  flight is never raced;
* `reconcile_terminal` advances a message whose job already finished but whose finalizer
  write was lost. §4.4 reads that column to let the next turn run, so without this half a
  crash at the wrong moment blocks the thread for ever.
"""
from __future__ import annotations

import argparse

from agento.framework.cli.runtime import _load_framework_config
from agento.framework.db import get_connection_or_exit

from . import relay, service


class ConversationSweepCommand:
    @property
    def name(self) -> str:
        return "conversation:sweep"

    @property
    def shortcut(self) -> str:
        return "co:sw"

    @property
    def help(self) -> str:
        return "Finish stranded conversation submissions and reconcile finished jobs"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            grace = service.config(conn, "sweep/pending_grace_seconds")
            completed = service.sweep_pending(conn, grace_seconds=grace)
            advanced = service.reconcile_terminal(conn)
            print(f"Completed {completed} pending message(s), advanced {advanced} to terminal.")
        finally:
            conn.close()


class ConversationRelayCommand:
    """`conversation:relay` — §6.4.1's single relay process, plus its own cleanup.

    One process, by contract: two would interleave and invert the order that
    `conversation_event.id` is supposed to give a thread.
    """

    @property
    def name(self) -> str:
        return "conversation:relay"

    @property
    def shortcut(self) -> str:
        return "co:rel"

    @property
    def help(self) -> str:
        return "Relay framework job events into conversation threads"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            relayed = relay.relay_outbox(conn)
            projected = relay.project_tool_calls(conn)
            pruned = relay.prune_relayed(
                conn, retention_days=service.config(conn, "retention/outbox_days"))
            print(f"Relayed {relayed} outbox row(s), projected {projected} tool call(s), "
                  f"pruned {pruned}.")
        finally:
            conn.close()


class ConversationRetentionCommand:
    """`conversation:retention` — §10.1's nightly pass: prune, retire, delete.

    In that order on purpose. Pruning first keeps the watermark current for every thread,
    including ones this pass is about to archive; deleting last means a thread archived
    seconds ago is not also deleted in the same run.
    """

    @property
    def name(self) -> str:
        return "conversation:retention"

    @property
    def shortcut(self) -> str:
        return "co:ret"

    @property
    def help(self) -> str:
        return "Prune conversation events, auto-archive idle threads, delete archived ones"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        from .retention import auto_archive, delete_archived, prune_events, prune_orphan_executions
        from .service import config

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            pruned = prune_events(conn, event_days=config(conn, "retention/event_days"))
            archived = auto_archive(conn, idle_days=config(conn, "retention/idle_days"))
            deleted = delete_archived(
                conn, archived_days=config(conn, "retention/archived_days"))
            orphans = prune_orphan_executions(
                conn, event_days=config(conn, "retention/event_days"))
            print(f"Pruned {pruned} event(s), archived {archived}, deleted {deleted}, "
                  f"removed {orphans} orphan execution(s).")
        finally:
            conn.close()
