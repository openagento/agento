"""CLI commands: ``capability:mint`` / ``capability:revoke``.

Operator-facing issuance for the two kinds a human can legitimately hold.
``mcp_job`` is deliberately unreachable: it belongs to a job the consumer runs,
its lifetime is bound to that job's terminal transition, and a hand-minted one
would outlive the job nothing revokes.
"""
from __future__ import annotations

import argparse
import sys

from ..db import get_connection_or_exit
from ..toolbox_capability import (
    INTERACTIVE_CAPABILITY_TTL_SECONDS,
    KIND_INTERNAL_REST,
    KIND_MCP_INTERACTIVE,
    REST_CAPABILITY_TTL_SECONDS,
    issue_capability,
    revoke_capability,
)
from .runtime import _load_framework_config

# Per kind the default IS the maximum: a longer-lived capability is a bigger
# blast radius, and there is no operator workflow that needs one.
_TTL_LIMITS = {
    KIND_INTERNAL_REST: REST_CAPABILITY_TTL_SECONDS,
    KIND_MCP_INTERACTIVE: INTERACTIVE_CAPABILITY_TTL_SECONDS,
}


class CapabilityMintCommand:
    @property
    def name(self) -> str:
        return "capability:mint"

    @property
    def shortcut(self) -> str:
        return "cap:mi"

    @property
    def help(self) -> str:
        return "Mint a toolbox capability for an agent_view (prints the token on stdout)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--kind", required=True, choices=sorted(_TTL_LIMITS),
            help="Capability kind. mcp_job is issued by the consumer only.",
        )
        parser.add_argument("--agent-view", required=True, dest="agent_view")
        parser.add_argument(
            "--ttl", type=int, default=None,
            help="Lifetime in seconds. Must be > 0 and <= the kind's maximum "
                 "(internal_rest 120, mcp_interactive 43200); the default is that maximum.",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from ..workspace import get_agent_view_by_code

        maximum = _TTL_LIMITS[args.kind]
        ttl = maximum if args.ttl is None else args.ttl
        # An out-of-range TTL is an ERROR, not a silent clamp: an operator who asked
        # for 24h and received 2min would deploy against the wrong assumption.
        if ttl <= 0 or ttl > maximum:
            print(
                f"Error: --ttl for {args.kind} must be between 1 and {maximum} seconds.",
                file=sys.stderr,
            )
            sys.exit(1)

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            av = get_agent_view_by_code(conn, args.agent_view)
            if av is None:
                print(f"Error: agent_view '{args.agent_view}' not found", file=sys.stderr)
                sys.exit(1)
            token = issue_capability(
                conn,
                kind=args.kind,
                agent_view_id=av.id,
                job_id=None,
                ttl_seconds=ttl,
            )
        finally:
            conn.close()

        # stdout is the token and nothing else, so `TOKEN=$(agento capability:mint …)`
        # works. Everything a human reads goes to stderr.
        print(
            f"Minted {args.kind} capability for agent_view '{args.agent_view}' "
            f"(ttl={ttl}s).",
            file=sys.stderr,
        )
        print(token)


class CapabilityRevokeCommand:
    @property
    def name(self) -> str:
        return "capability:revoke"

    @property
    def shortcut(self) -> str:
        return "cap:re"

    @property
    def help(self) -> str:
        return "Revoke a toolbox capability — reads the raw token from stdin"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        return None

    def execute(self, args: argparse.Namespace) -> None:
        # The token arrives on stdin, never as an argument: argv is world-readable
        # in `ps` and lands in shell history. There is deliberately no --id flag —
        # the table stores hashes only, so an id would need a listing that
        # correlates operators with live capabilities for no gain.
        token = sys.stdin.readline().strip()
        if not token:
            print("No token provided on stdin.", file=sys.stderr)
            sys.exit(1)

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            revoked = revoke_capability(conn, token)
        finally:
            conn.close()

        if not revoked:
            print("No live capability matches that token.", file=sys.stderr)
            sys.exit(1)
        print("Capability revoked.", file=sys.stderr)
