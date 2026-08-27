"""CLI command: agent_view:identity:show — display SSH identity info (never the private key)."""
from __future__ import annotations

import argparse
import sys

from agento.framework.ssh_keys import check_keypair


class IdentityShowCommand:
    @property
    def name(self) -> str:
        return "agent_view:identity:show"

    @property
    def shortcut(self) -> str:
        return "av:id:sh"

    @property
    def help(self) -> str:
        return "Show stored SSH identity (public key + fingerprint; private key is never dumped)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("agent_view_code", help="Agent view code")

    def execute(self, args: argparse.Namespace) -> None:
        from agento.framework.cli.runtime import _load_framework_config
        from agento.framework.config_resolver import DecryptError, ScopedConfigService
        from agento.framework.db import get_connection_or_exit
        from agento.framework.workspace import get_agent_view_by_code

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            av = get_agent_view_by_code(conn, args.agent_view_code)
            if av is None:
                print(
                    f"Error: agent_view '{args.agent_view_code}' not found",
                    file=sys.stderr,
                )
                sys.exit(1)
            # ScopedConfigService applies the documented ENV -> DB -> config.json
            # fallback and decrypts as it goes. build_scoped_overrides saw DB rows
            # ONLY, so an identity supplied by env var or config.json — the same
            # one workspace_build will materialize — reported as "not stored".
            # Four per-path reads, never resolve_all().
            svc = ScopedConfigService(conn, "agent_view", av.id)
            try:
                private = svc.get(
                    "agent_view/identity/ssh_private_key", strict=True
                )
                decrypt_failed = False
            except DecryptError:
                # strict=True is what makes this reachable. Without it the
                # resolver swallows the failure and falls through to
                # config.json, so an unreadable key printed as "not stored" —
                # the same silence the incident had.
                private, decrypt_failed = None, True
            public = svc.get("agent_view/identity/ssh_public_key")
            ssh_config = svc.get("agent_view/identity/ssh_config")
            known_hosts = svc.get("agent_view/identity/ssh_known_hosts")
        finally:
            conn.close()

        if not private and not public and not decrypt_failed:
            print(f"No SSH identity stored for agent_view '{av.code}'")
            return

        print(f"agent_view: {av.code} (id={av.id})")
        if decrypt_failed:
            print("  private key: stored (unable to decrypt)")
        elif private:
            check = check_keypair(private, public)
            print(f"  private key: stored — {check.code}: {check.detail}")
        if public:
            print(f"  public key:  {public.strip()}")
        if ssh_config:
            lines = ssh_config.strip().splitlines()
            preview = "; ".join(lines[:3])
            more = "" if len(lines) <= 3 else f" (+{len(lines) - 3} more lines)"
            print(f"  ssh config:  {preview}{more}")
        if known_hosts:
            host_lines = known_hosts.strip().splitlines()
            plural = "y" if len(host_lines) == 1 else "ies"
            print(f"  known_hosts: {len(host_lines)} entr{plural}")
