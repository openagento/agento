"""CLI command: artifacts:prune — remove old run dirs and thread dirs (docs/cli/artifacts-prune.md)."""
from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_LOCK = "artifacts_prune"


class ArtifactsPruneCommand:
    @property
    def name(self) -> str:
        return "artifacts:prune"

    @property
    def shortcut(self) -> str:
        return "ar:pr"

    @property
    def help(self) -> str:
        return "Remove run dirs and thread dirs of jobs that ended long ago (sessions in state/ stay)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--dry-run", action="store_true",
                            help="Remove nothing; report what would go")
        parser.add_argument("--print", dest="print_files", action="store_true",
                            help="List each file removed (with --dry-run: each file that would go)")
        when = parser.add_mutually_exclusive_group()
        when.add_argument("--days", type=int, default=None,
                          help="Override core/artifacts/retention_days (0 keeps everything)")
        when.add_argument("--before", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
                          help="Remove what ended before this date (00:00 UTC), not after today")

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.artifacts_retention import (
            PruneReport,
            prune_run_dirs,
            prune_thread_dirs,
        )
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.db import get_connection

        now = datetime.now(UTC).replace(tzinfo=None)
        if args.before is not None and args.before > now.date():
            raise SystemExit(f"--before {args.before} is after today (UTC)")
        db_config, _, _ = _load_framework_config()
        conn = get_connection(db_config)
        locked = False
        try:
            with conn.cursor() as cur:
                # Thread-dir retention re-reads job rows after a lock wait (prune_thread_dirs).
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
                cur.execute("SELECT GET_LOCK(%s, 0) AS got", (_LOCK,))
                locked = bool((cur.fetchone() or {}).get("got"))
            if not locked:
                print("another artifacts:prune is running")
                return
            if args.before is not None:
                cutoff = datetime.combine(args.before, datetime.min.time())
            else:
                days = args.days if args.days is not None else _configured_days(conn)
                cutoff = now - timedelta(days=days) if days > 0 else None
            verb = "would remove" if args.dry_run else "removed"
            on_file = (lambda p: print(f"{verb} {p}")) if args.print_files else None
            on_kept = (lambda p, why: print(f"kept {p} ({why})")) if args.print_files else None
            report = PruneReport()
            for prune in (prune_run_dirs, prune_thread_dirs) if cutoff else ():
                prune(conn, cutoff=cutoff, dry_run=args.dry_run, report=report,
                      on_file=on_file, on_kept=on_kept)
            kept = ",".join(f"{k}:{n}" for k, n in sorted(report.kept.items()))
            print(f"{'dry-run: ' if args.dry_run else ''}run_dirs={report.run_dirs} "
                  f"thread_dirs={report.thread_dirs} bytes={report.bytes} kept={kept}")
        finally:
            if locked:
                with conn.cursor() as cur:
                    cur.execute("SELECT RELEASE_LOCK(%s)", (_LOCK,))
            conn.close()


def _configured_days(conn) -> int:
    from agento.framework.bootstrap import CORE_MODULES_DIR
    from agento.framework.config_resolver import (
        load_db_overrides,
        read_config_defaults,
        resolve_field,
    )

    core = Path(CORE_MODULES_DIR) / "core"
    schema = json.loads((core / "system.json").read_text())["artifacts/retention_days"]
    return int(resolve_field("core", "artifacts/retention_days", schema,
                             read_config_defaults(core), load_db_overrides(conn)).value)
