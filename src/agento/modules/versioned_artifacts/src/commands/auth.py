"""CLI command: artifact:auth — set, rotate, show or disable an artifact's Basic auth."""
from __future__ import annotations

import argparse
import sys

from ._toolbox import compose_flags, fail_on_error, run_toolbox


class VersionedArtifactAuthCommand:
    @property
    def name(self) -> str:
        return "artifact:auth"

    @property
    def shortcut(self) -> str:
        # No alias: see artifact:list.
        return ""

    @property
    def help(self) -> str:
        return "Set, rotate, show or disable an artifact's HTTP Basic auth"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code, e.g. openagento-website")
        parser.add_argument("--user", default=None, help="Basic auth user (default: the artifact code)")
        parser.add_argument("--pass", dest="password", default=None,
                            help="Basic auth password (default: a strong random one)")
        parser.add_argument("--disable", action="store_true", help="Turn Basic auth off")
        parser.add_argument("--show", action="store_true", help="Show the current credential instead of changing it")
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        flags = compose_flags()

        if args.show:
            body, result = run_toolbox(
                flags, ["--op", "auth-show", "--actor", args.actor],
                {"artifact_code": args.artifact_code})
            fail_on_error(body, result, "could not read the credential")
            if not body.get("auth_enabled"):
                print(f"Basic auth is OFF for '{args.artifact_code}'.")
                return
            print(f"Basic auth for '{args.artifact_code}':")
            print(f"  user:     {body.get('auth_user')}")
            print(f"  password: {body.get('password')}")
            return

        body, result = run_toolbox(
            flags, ["--op", "auth", "--actor", args.actor],
            {"artifact_code": args.artifact_code, "user": args.user,
             "password": args.password, "disable": args.disable})
        fail_on_error(body, result, "could not change the credential")

        if args.disable:
            if body.get("auth_enabled") is not False:
                print("Error: FAILED: the toolbox did not confirm the change", file=sys.stderr)
                raise SystemExit(1)
            print(f"Basic auth disabled for '{args.artifact_code}'.")
            return

        if not body.get("auth_enabled") or not body.get("password"):
            print("Error: FAILED: the toolbox did not confirm the credential", file=sys.stderr)
            raise SystemExit(1)
        print(f"Basic auth enabled for '{args.artifact_code}':")
        print(f"  user:     {body.get('auth_user')}")
        print(f"  password: {body.get('password')}")
        print("Pass these to whoever needs to open the preview; the password is shown only now unless you --show it.")
