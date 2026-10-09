"""Make the runner server unreadable through ``/proc`` to a same-uid peer.

The runner server (``framework/runner/server.py``) holds the run secrets of every
co-tenant run in memory: credential env, SSH key, capability token. Its agents run as
the SAME uid, and a same-uid ptrace-mode read needs only that the target is
*dumpable*. ``PR_SET_DUMPABLE=0`` withdraws that: the kernel reparents the process's
``/proc`` entries to root and denies ptrace-mode access, and because these containers
drop ``CAP_SYS_PTRACE`` nothing in the container can override it.

This is process hardening, not a boundary between agent_views (one uid, one
``/workspace``). The worker (cron) does not call it: no agent runs there.

Stated limits: ``execve`` resets dumpable to 1, so a process that execs and then calls
this has a short window. Side effects: no core dumps, and the process cannot read its
own ``/proc/self/fd``, so CPython's descriptor closing falls back from the ``/proc``
scan to ``close_range``/a brute-force loop.
"""
from __future__ import annotations

import sys

PR_SET_DUMPABLE = 4


def _load_libc():
    """The seam: returns libc, or None where prctl does not exist."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes
    import ctypes.util

    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:
        return None


def make_non_dumpable() -> bool:
    """Withdraw ptrace-mode access to this process. True when it took effect.

    Never raises: a process that cannot harden itself must still run — the
    alternative is a consumer that refuses to start on a platform without
    ``prctl``, which protects nothing.
    """
    libc = _load_libc()
    if libc is None:
        return False
    try:
        return libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False
