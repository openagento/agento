"""CLI command: ingress:list — list all ingress identity bindings."""
from __future__ import annotations

import argparse
import json


class IngressListCommand:
    @property
    def name(self) -> str:
        return "ingress:list"

    @property
    def shortcut(self) -> str:
        return "in:li"

    @property
    def help(self) -> str:
        return "List ingress identity bindings"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--type", dest="identity_type", help="Filter by identity type")
        parser.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.db import get_connection
        from agento.framework.ingress_identity import list_identities

        db_config, _, _ = _load_framework_config()
        conn = get_connection(db_config)
        try:
            identities = list_identities(conn, identity_type=args.identity_type)
            if args.as_json:
                rows = [
                    {
                        "id": i.id,
                        "identity_type": i.identity_type,
                        "identity_value": i.identity_value,
                        "agent_view_id": i.agent_view_id,
                        "priority": i.priority,
                        "is_active": i.is_active,
                    }
                    for i in identities
                ]
                print(json.dumps(rows, indent=2))
            else:
                if not identities:
                    print("No ingress identities found.")
                    return
                for i in identities:
                    status = "active" if i.is_active else "inactive"
                    print(
                        f"  {i.identity_type}={i.identity_value} → agent_view_id={i.agent_view_id} "
                        f"[priority={i.priority}, {status}]"
                    )
        finally:
            conn.close()
