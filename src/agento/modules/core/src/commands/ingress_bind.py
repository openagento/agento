"""CLI command: ingress:bind — bind an inbound identity to an agent_view."""
from __future__ import annotations

import argparse
import sys

import regex

# Best-effort footgun lint (NOT a ReDoS proof): a group whose body contains a quantifier and is
# itself quantified, e.g. (a+)+. Incomplete by design — it does NOT catch alternation-overlap
# exponentials like (?:a|aa)+; the real runtime bound is the §2 matcher `regex` timeout, which
# protects every caller (incl. direct DB inserts). Linear pattern on the admin-authored pattern
# STRING, so it cannot itself backtrack.
_NESTED_QUANTIFIER = regex.compile(r"\([^)]*[*+{][^)]*\)[*+{]", regex.VERSION0)


def _looks_like_nested_quantifier(value: str) -> bool:
    return _NESTED_QUANTIFIER.search(value) is not None


class IngressBindCommand:
    @property
    def name(self) -> str:
        return "ingress:bind"

    @property
    def shortcut(self) -> str:
        return "in:bi"

    @property
    def help(self) -> str:
        return "Bind an inbound identity to an agent_view"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("type", help="Identity type (e.g. jira, api_client, outlook_sender)")
        parser.add_argument("value", help="Identity value; a regex for regex types (e.g. outlook_sender)")
        parser.add_argument("agent_view_code", help="Agent view code to bind to")
        parser.add_argument(
            "--priority", type=int, default=None,
            help="Selection priority for regex types (higher wins; default 0 on first bind)",
        )

    def _validate_regex(self, value: str) -> None:
        """Validate a regex identity value with the SAME engine + version the runtime matcher uses
        (`regex`, VERSION0) so a pattern accepted here behaves identically at match time. Exits
        non-zero on failure, BEFORE any DB write. Never echoes the raw pattern into shared logs."""
        if not value:
            print("Error: regex pattern must not be empty.")
            sys.exit(1)
        if len(value) > 255:
            print("Error: regex pattern too long (max 255 characters).")
            sys.exit(1)
        try:
            regex.compile(value, regex.VERSION0 | regex.IGNORECASE)
        except regex.error:
            print(r"Error: invalid regex pattern (failed to compile). Example: [^@]+@company\.com")
            sys.exit(1)
        if _looks_like_nested_quantifier(value):
            print(
                "Error: pattern has nested unbounded quantifiers (ReDoS footgun, e.g. (a+)+). "
                r"Use a simpler anchored form, e.g. [^@]+@company\.com"
            )
            sys.exit(1)

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.db import get_connection
        from agento.framework.ingress_identity import bind_identity, is_regex_identity_type
        from agento.framework.workspace import get_agent_view_by_code

        # Regex types are validated BEFORE any DB access (bind_identity commits). Gating on the
        # module-owned registry (populated by bootstrap in main() phase 1), never a literal.
        if is_regex_identity_type(args.type):
            self._validate_regex(args.value)

        db_config, _, _ = _load_framework_config()
        conn = get_connection(db_config)
        try:
            agent_view = get_agent_view_by_code(conn, args.agent_view_code)
            if agent_view is None:
                print(f"Error: agent_view '{args.agent_view_code}' not found")
                return
            bind_identity(conn, args.type, args.value, agent_view.id, priority=args.priority)
            priority_note = f" priority={args.priority}" if args.priority is not None else ""
            print(
                f"Bound {args.type}={args.value} → agent_view '{args.agent_view_code}' "
                f"(id={agent_view.id}){priority_note}"
            )
        finally:
            conn.close()
