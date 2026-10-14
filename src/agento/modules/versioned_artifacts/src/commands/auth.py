"""CLI command: artifact:auth — set, rotate or disable an artifact's Basic auth.

It runs in cron and asks the toolbox on the ``/admin`` endpoint (D-MINIAPP-2). The password
goes to this terminal only: no copy is kept that can be read back (O-4).
"""
from __future__ import annotations

import argparse
import getpass
import sys

from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.web import app_path, toolbox_client


class VersionedArtifactAuthCommand:
    @property
    def name(self) -> str:
        return "artifact:auth"

    @property
    def shortcut(self) -> str:
        return "ar:au"

    @property
    def help(self) -> str:
        return "Set, rotate or disable an artifact's HTTP Basic auth"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code, e.g. openagento-website")
        parser.add_argument("--user", default=None, help="Basic auth user (default: the artifact code)")
        parser.add_argument("--pass-stdin", action="store_true",
                            help="Read the password from stdin, or a prompt on a terminal "
                                 "(default: a strong random one)")
        parser.add_argument("--disable", action="store_true", help="Turn Basic auth off")
        parser.add_argument("--actor", default="admin", help="Who is running this command (audit)")

    def execute(self, args: argparse.Namespace) -> None:
        code = args.artifact_code
        if not app_path.ARTIFACT_CODE_RE.fullmatch(code):
            _fail("INVALID_ARTIFACT_CODE", "the artifact code must match ^[a-z0-9][a-z0-9-]{0,63}$")
        # Collect the input first, then mint: the capability lives for one request.
        password = _read_password() if args.pass_stdin else None
        conn = get_connection(DatabaseConfig.from_env())
        try:
            r = toolbox_client.admin_request(
                conn, "POST", f"versioned-artifacts/{code}/auth", subject_id=f"service:cli:{args.actor}",
                body={"user": args.user, "password": password, "disable": args.disable})
        finally:
            conn.close()
        body = r.body
        if r.status != 200:
            _fail(body.get("error_code", "FAILED"), body.get("message") or "could not change the credential")

        if args.disable:
            if body.get("auth_enabled") is not False:
                _fail("FAILED", "the toolbox did not confirm the change")
            print(f"Basic auth disabled for '{code}'.")
            return

        if not body.get("auth_enabled") or not body.get("password"):
            _fail("FAILED", "the toolbox did not confirm the credential")
        print(f"Basic auth enabled for '{code}':")
        print(f"  user:     {body.get('auth_user')}")
        print(f"  password: {body.get('password')}")
        url = body.get("share_url")
        print(f"  share:    {url}" if url else "  share:    not configured (AGENTO_SHARE_HOST is empty or invalid)")
        print("Pass these to whoever needs to open the preview; the password is shown only now.")


def _fail(error_code: str, message: str) -> None:
    print(f"Error: {error_code}: {message}", file=sys.stderr)
    raise SystemExit(1)


def _read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            _fail("FAILED", "passwords do not match")
    else:
        password = sys.stdin.read().rstrip("\r\n")
    if not password:
        _fail("FAILED", "the password is empty")
    return password
