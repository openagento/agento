"""The miniapp:* commands run on the host and exec this module's toolbox CLI.

The reply contract is versioned_artifacts' one (a declared dependency), not a copy.
"""
from __future__ import annotations

import argparse

from agento.modules.versioned_artifacts.src.commands._toolbox import compose_flags, fail_on_error, run_toolbox

TOOLBOX_CLI = "/app/modules/core/miniapps/toolbox/cli.js"


def call(op: str, args: argparse.Namespace, payload: dict, fallback: str) -> dict:
    body, result = run_toolbox(compose_flags(), ["--op", op, "--actor", args.actor], payload, cli=TOOLBOX_CLI)
    fail_on_error(body, result, fallback)
    return body
