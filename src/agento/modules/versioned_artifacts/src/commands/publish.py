"""CLI command: artifact:publish — point an artifact's current at an existing version."""
from __future__ import annotations

import argparse
import sys

from ._toolbox import compose_flags, fail_on_error, run_toolbox


class VersionedArtifactPublishCommand:
    @property
    def name(self) -> str:
        return "artifact:publish"

    @property
    def shortcut(self) -> str:
        # No alias: see artifact:list.
        return ""

    @property
    def help(self) -> str:
        return "Publish a version of an artifact (operator equivalent of versioned_artifact_publish)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("artifact_code", help="Artifact code, e.g. openagento-website")
        parser.add_argument("version_id", help="The version to publish")
        # REQUIRED, never defaulted: a default would silently disable the
        # optimistic-concurrency guard, and the repair call for a stale served tree
        # is this same command with --expected equal to version_id.
        parser.add_argument(
            "--expected", required=True,
            help="The version you believe is current; publish fails if it changed")
        parser.add_argument("--actor", default="admin", help="Who is running this command")

    def execute(self, args: argparse.Namespace) -> None:
        flags = compose_flags()
        body, result = run_toolbox(
            flags, ["--op", "publish", "--actor", args.actor],
            {"artifact_code": args.artifact_code, "version_id": args.version_id,
             "expected_current_version": args.expected})
        fail_on_error(body, result, "the publish failed")
        if body.get("current_version") != args.version_id:
            print("Error: FAILED: the toolbox did not confirm the publish", file=sys.stderr)
            raise SystemExit(1)
        print(f"Published '{args.artifact_code}' at version {args.version_id} "
              f"(was {body.get('previous_version')})")
        if body.get("preview_url"):
            print(f"Preview: {body['preview_url']}")
        if body.get("preview_stale"):
            # The store moved and the served tree did not. The repair is this same
            # command with --expected equal to the version just published.
            print(f"Warning: the served tree still shows the previous version; repair it with "
                  f"`agento artifact:publish {args.artifact_code} {args.version_id} "
                  f"--expected {args.version_id}`")
