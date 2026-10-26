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
import time
from datetime import UTC, date, datetime, timedelta

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


RELAY_PASS_S = 1
RELAY_BUDGET_S = 55   # under the 60 s cron period, so invocations seldom overlap
RELAY_LOCK = "conversation_relay"


def run_relay(conn, *, clock=time.monotonic, sleep=time.sleep):
    """One cron invocation: relay passes every `RELAY_PASS_S` until `RELAY_BUDGET_S` ends.

    Returns `(relayed, projected, pruned)`, or `None` when another invocation holds the
    lock. The named lock keeps the one-relay contract when two invocations overlap: the
    second exits at once and never waits.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 0) AS got", (RELAY_LOCK,))
        if cur.fetchone()["got"] != 1:
            return None
    try:
        deadline = clock() + RELAY_BUDGET_S
        relayed = projected = 0
        while True:
            relayed += relay.relay_outbox(conn)
            projected += relay.project_tool_calls(conn)
            if clock() + RELAY_PASS_S >= deadline:
                break
            sleep(RELAY_PASS_S)
        pruned = relay.prune_relayed(
            conn, retention_days=service.config(conn, "retention/outbox_days"))
        return relayed, projected, pruned
    finally:
        with conn.cursor() as cur:
            cur.execute("DO RELEASE_LOCK(%s)", (RELAY_LOCK,))


class ConversationRelayCommand:
    """`conversation:relay` — §6.4.1's single relay process, plus its own cleanup.

    One process, by contract: two would interleave and invert the order that
    `conversation_event.id` is supposed to give a thread. `run_relay` holds a named lock
    for the whole invocation to keep it so.
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
            totals = run_relay(conn)
            if totals is None:
                print("Another conversation:relay is running; exiting.")
                return
            relayed, projected, pruned = totals
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
        from .retention import auto_archive, delete_archived, prune_events, prune_old_executions
        from .service import config

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            pruned = prune_events(conn, event_days=config(conn, "retention/event_days"))
            archived = auto_archive(conn, idle_days=config(conn, "retention/idle_days"))
            deleted = delete_archived(
                conn, archived_days=config(conn, "retention/archived_days"))
            runs = prune_old_executions(
                conn, event_days=config(conn, "retention/event_days"))
            print(f"Pruned {pruned} event(s), archived {archived}, deleted {deleted}, "
                  f"removed {runs} old execution(s).")
        finally:
            conn.close()


class ConversationBackfillCommand:
    """`conversation:backfill` — imports the jobs that ended with no conversation run (backfill.py)."""

    @property
    def name(self) -> str:
        return "conversation:backfill"

    @property
    def shortcut(self) -> str:
        return "co:ba"

    @property
    def help(self) -> str:
        return "Import finished jobs that have no conversation run into their conversations"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--source", help="Comma-separated job sources, e.g. jira,outlook")
        parser.add_argument("--agent-view", help="Only the jobs of this agent_view code")
        parser.add_argument("--since", type=date.fromisoformat, help="YYYY-MM-DD; default: "
                            "now - retention/event_days, all jobs when that is 0")
        parser.add_argument("--limit", type=_positive, help="Import at most N jobs")
        parser.add_argument("--dry-run", action="store_true",
                            help="Count what would be imported and write nothing")

    def execute(self, args: argparse.Namespace) -> None:
        from .backfill import EXCLUDED_SOURCES, run

        sources = [s.strip() for s in args.source.split(",") if s.strip()] if args.source else None
        excluded = sorted(set(sources or ()) & set(EXCLUDED_SOURCES))
        if excluded:
            raise SystemExit(f"--source: {', '.join(excluded)} is never imported")
        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            if args.since:
                since = datetime.combine(args.since, datetime.min.time())
            else:
                days = service.config(conn, "retention/event_days")
                since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days) \
                    if days > 0 else None
            report = run(conn, sources=sources, agent_view=args.agent_view, since=since,
                         limit=args.limit, dry_run=args.dry_run)
        finally:
            conn.close()
        if args.dry_run:
            print("Dry run: nothing was written.")
        print(f"{'source':<16}{'transcript':>11}{'fallback':>9}{'errors':>7}"
              f"{'has run':>8}{'no view':>8}{'window':>7}")
        for source in sorted({*report.transcript, *report.fallback, *report.errors,
                              *report.skipped}):
            skipped = report.skipped.get(source, {})
            print(f"{source:<16}{report.transcript[source]:>11}{report.fallback[source]:>9}"
                  f"{report.errors[source]:>7}{skipped.get('has_execution', 0):>8}"
                  f"{skipped.get('no_view', 0):>8}{skipped.get('outside_window', 0):>7}")


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number
