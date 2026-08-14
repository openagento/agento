"""CLI command: github:publish-comments — sweep open PRs for unanswered reviewer feedback (~2h)."""
from __future__ import annotations

import argparse

from ..channel import LANE_COMMENTS
from ._loop import configure_lane_parser, execute_lane


class GitHubPublishCommentsCommand:
    @property
    def name(self) -> str:
        return "github:publish-comments"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Sweep open GitHub PRs and publish jobs for unanswered reviewer feedback"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        configure_lane_parser(parser)

    def execute(self, args: argparse.Namespace) -> None:
        execute_lane(LANE_COMMENTS, args)
