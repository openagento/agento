"""Build a ``docker compose exec`` into the **cron** service.

The cron service holds the credential store in its own environment (compose passes
``MYSQL_*`` and the project's ``secrets.env``), so an ``exec -u agent cron …`` would hand
that environment straight to the command — bypassing the launcher that clears it. Every
cron exec therefore enters as **root** and drops privileges through the launcher, exactly
as the entrypoint and the crontab do.

This is only for the ``cron`` service. The ``sandbox`` service (``cli/run.py``) runs the
agent itself, has no launcher and no store to withhold, and keeps ``-u agent``.
"""
from __future__ import annotations

from collections.abc import Sequence

LAUNCHER = "/opt/cron-agent/launch.sh"


def cron_exec(
    argv: Sequence[str],
    *,
    tty: str = "-T",
    env_flags: Sequence[str] = (),
    store: bool = True,
) -> list[str]:
    """The ``exec …`` part of a compose invocation, to append after the compose flags.

    ``store=False`` is for a command that is not the framework CLI (a probe such as
    ``test -f``): it still enters through the launcher, but no file descriptor is opened.
    """
    return [
        "exec", "-u", "root", tty, *env_flags, "cron",
        LAUNCHER, *(["--store"] if store else []), "--", *argv,
    ]
