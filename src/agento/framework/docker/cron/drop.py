#!/usr/bin/env python3
"""Read the credential store as root, drop to uid `agent`, then run the CLI in-process.

`execve` resets `PR_SET_DUMPABLE` to 1, and these containers have no yama
`ptrace_scope`, so from the exec until the new image calls `prctl` again a same-uid
peer can `PTRACE_ATTACH` the process and make it read anything it holds. A store handed
ACROSS an exec — on a file descriptor, in argv, in the environment — is therefore
reachable for the whole of Python's startup, which is the one window the 0600 file mode
does not cover.

So the store is never handed across an exec. This program is started by
`/opt/cron-agent/launch.sh --store` as root, reads the file while it still may, drops
every id to `agent`, makes itself non-dumpable, and only then imports the framework and
calls the CLI. There is no exec after the drop, so there is no window.

Root-owned, mode 0700: uid `agent` cannot read, edit or run it.
"""
from __future__ import annotations

import grp
import os
import pwd
import sys

STORE_FILE = "/opt/cron-agent/env"
AGENT_USER = "agent"


def _drop_to(user: str) -> None:
    pw = pwd.getpwnam(user)
    groups = sorted({g.gr_gid for g in grp.getgrall() if user in g.gr_mem} | {pw.pw_gid})
    os.setgroups(groups)
    os.setresgid(pw.pw_gid, pw.pw_gid, pw.pw_gid)
    os.setresuid(pw.pw_uid, pw.pw_uid, pw.pw_uid)
    # Fail closed: a drop that silently did not happen would run the agent's own command
    # as root. There is no legitimate path back to uid 0 from here (setresuid clears the
    # saved id too), so this is the last point at which the check is possible.
    if os.getuid() != pw.pw_uid or os.geteuid() != pw.pw_uid or os.getgid() != pw.pw_gid:
        sys.exit("drop.py: privilege drop did not take effect")


def main(argv: list[str]) -> int:
    with open(STORE_FILE, "rb") as handle:
        raw = handle.read()

    _drop_to(AGENT_USER)

    from agento.framework.process_hardening import make_non_dumpable

    make_non_dumpable()

    from agento.framework import store_env

    store_env.load(raw)

    from agento.framework.cli import main as cli_main

    sys.argv = ["agento", *argv]
    cli_main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
