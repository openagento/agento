"""CLI command: github:publish-changes — fast lane for reviewer 'changes requested' (~1m)."""
from __future__ import annotations

import argparse

from ..channel import LANE_CHANGES
from ._loop import configure_lane_parser, execute_lane


class GitHubPublishChangesCommand:
    @property
    def name(self) -> str:
        return "github:publish-changes"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Publish jobs for GitHub PRs where a reviewer requested changes"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        configure_lane_parser(parser)

    def execute(self, args: argparse.Namespace) -> None:
        execute_lane(LANE_CHANGES, args)
