"""CLI command: config:test — run the tester declared on a config field.

Exit codes: 0 = every run ok (or only not_configured), 1 = a run failed or
errored, 2 = usage problem (no path, or the field declares no tester).
"""
from __future__ import annotations

import argparse
import sys

from ..config_test import ERROR, FAIL, NOT_CONFIGURED, OK
from ..config_test.manifest import enumerate_test_groups, tester_for_field
from ..config_test.runner import run_config_test
from ..scoped_config import Scope

_LABEL = {
    OK: "OK",
    FAIL: "FAIL",
    ERROR: "ERROR",
    NOT_CONFIGURED: "NOT_CONFIGURED",
}


def _open_connection():
    from ..db import get_connection_or_exit
    from .runtime import _load_framework_config

    db_config, _, _ = _load_framework_config()
    return get_connection_or_exit(db_config)


def _resolve_scope(conn, args) -> tuple[str, int]:
    from .config import _resolve_scope_from_args

    return _resolve_scope_from_args(conn, args)


def _print(path: str, result) -> None:
    label = _LABEL.get(result.status, result.status.upper())
    code = f" [{result.code}]" if result.code and result.code != label else ""
    print(f"{label}{code}  {path}: {result.message}")


class ConfigTestCommand:
    @property
    def name(self) -> str:
        return "config:test"

    @property
    def shortcut(self) -> str:
        return "co:te"

    @property
    def help(self) -> str:
        return (
            "Test a config field's live connection or credential "
            "(exit 1 on failure; an unconfigured integration exits 0)"
        )

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path", nargs="?",
            help="Config path whose field declares a tester (e.g. core/smtp_pass)",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Run every field that declares a tester, once, at this scope",
        )
        parser.add_argument("--scope", default=Scope.DEFAULT)
        parser.add_argument("--scope-id", type=int, default=0, dest="scope_id")
        parser.add_argument(
            "--agent-view", dest="agent_view",
            help="Run at this agent_view's scope (by code)",
        )

    def execute(self, args: argparse.Namespace) -> None:
        if not args.all and not args.path:
            print(
                "Error: give a config path or --all (see `agento config:test --help`)",
                file=sys.stderr,
            )
            sys.exit(2)

        # Both usage errors are decided from the manifests alone, before a DB
        # connection is opened: a mistyped path must not look like a test that ran.
        if args.all:
            # Groups, not fields: one probe per distinct declaration. Six Graph
            # fields sharing one named probe are one login, not six.
            groups = enumerate_test_groups()
            if not groups:
                print("No config field declares a tester in the enabled modules.")
                sys.exit(0)
        else:
            if tester_for_field(args.path) is None:
                print(
                    f"Error: '{args.path}' declares no tester in its module's "
                    f"system.json",
                    file=sys.stderr,
                )
                sys.exit(2)
            groups = [(args.path, (args.path,))]

        conn = _open_connection()
        try:
            scope, scope_id = _resolve_scope(conn, args)
            all_ok = True
            for path, shared in groups:
                result = run_config_test(
                    conn, path, scope=scope, scope_id=scope_id
                )
                _print(path, result)
                for other in shared[1:]:
                    print(f"  {other}: same test as {path}")
                if result.status in (FAIL, ERROR):
                    all_ok = False
        finally:
            conn.close()
        sys.exit(0 if all_ok else 1)
