"""CLI command: agent_view:identity:check — verify the stored SSH identity.

Prints a machine-readable code plus one detail line, never the private key.
Exit 0 only for OK, so it can gate a deploy.
"""
from __future__ import annotations

import argparse
import sys

from agento.framework.config_resolver import DecryptError
from agento.framework.ssh_keys import CODE_OK, check_keypair

PRIVATE_PATH = "agent_view/identity/ssh_private_key"
PUBLIC_PATH = "agent_view/identity/ssh_public_key"


def _resolve_identity(agent_view_code: str):
    """``(agent_view, private, public)`` for one view. Exits 1 if it is unknown.

    Resolution goes through ``ScopedConfigService``, which applies the documented
    three-level fallback ENV -> DB (decrypted) -> config.json. ``build_scoped_
    overrides`` would see DB rows ONLY, so a key provided by env var or
    config.json would read as NOT_SET while ``workspace_build`` materializes it
    happily — the checker must resolve the value the build actually uses.

    Split out so the tests can stub one DB seam.
    """
    from agento.framework.cli.runtime import _load_framework_config
    from agento.framework.config_resolver import ScopedConfigService
    from agento.framework.db import get_connection_or_exit
    from agento.framework.workspace import get_agent_view_by_code

    db_config, _, _ = _load_framework_config()
    conn = get_connection_or_exit(db_config)
    try:
        av = get_agent_view_by_code(conn, agent_view_code)
        if av is None:
            print(f"Error: agent_view '{agent_view_code}' not found", file=sys.stderr)
            sys.exit(1)
        svc = ScopedConfigService(conn, "agent_view", av.id)
        # Two per-path reads, never resolve_all() — testing one identity must not
        # decrypt every module's secrets. strict=True on the private key so an
        # undecryptable row raises DecryptError instead of falling through to
        # config.json and reporting NOT_SET (Task 2, Step 6b). The public key is
        # not encrypted, so the lenient read is right for it.
        return av, svc.get(PRIVATE_PATH, strict=True), svc.get(PUBLIC_PATH)
    finally:
        conn.close()


class IdentityCheckCommand:
    @property
    def name(self) -> str:
        return "agent_view:identity:check"

    @property
    def shortcut(self) -> str:
        return "av:id:ch"

    @property
    def help(self) -> str:
        return (
            "Verify the stored SSH identity parses and forms a pair with the stored "
            "public key (exit 0 only on OK; the private key is never printed)"
        )

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("agent_view_code", help="Agent view code")

    def execute(self, args: argparse.Namespace) -> None:
        try:
            av, private, public = _resolve_identity(args.agent_view_code)
        except SystemExit:
            raise
        except DecryptError:
            # The one failure with a real diagnosis: a stored row exists and did
            # not decrypt, so the value was written under a different
            # AGENTO_ENCRYPTION_KEY. Reported as its own outcome rather than as
            # INVALID_KEY, which would send the operator off to regenerate a key
            # that is fine. DecryptError carries the path only.
            print(
                "  DECRYPT_FAILED — the stored private key could not be "
                "decrypted (is AGENTO_ENCRYPTION_KEY the one it was stored "
                "with?)"
            )
            sys.exit(1)
        except Exception as e:
            # Anything else — the DB is down, a manifest is broken, a resolver
            # bug. Do NOT dress it up as DECRYPT_FAILED: the operator would go
            # hunting an encryption key for a connection error. Type name only:
            # a resolver exception message can quote the value it was handling.
            print(f"  CHECK_FAILED — could not read the stored identity: {type(e).__name__}")
            sys.exit(1)

        check = check_keypair(private, public)
        print(f"agent_view: {av.code}")
        print(f"  {check.code} — {check.detail}")
        sys.exit(0 if check.code == CODE_OK else 1)
