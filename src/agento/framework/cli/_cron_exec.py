"""Build a ``docker compose exec`` into the **cron** service.

An ``exec -u agent cron …`` would hand the exec's own environment straight to the command.
Every cron exec therefore enters as **root** and drops privileges through the launcher,
which builds the environment from the persisted whitelist, exactly as the entrypoint and
the crontab do.

This is only for the ``cron`` service. The ``sandbox`` service (``cli/run.py``) has no
launcher and keeps ``-u agent``.
"""
from __future__ import annotations

from collections.abc import Sequence

LAUNCHER = "/opt/cron-agent/launch.sh"


def cron_exec(argv: Sequence[str], *, tty: str = "-T", env_flags: Sequence[str] = ()) -> list[str]:
    """The ``exec …`` part of a compose invocation, to append after the compose flags."""
    return ["exec", "-u", "root", tty, *env_flags, "cron", LAUNCHER, "--", *argv]
