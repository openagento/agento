"""Make a framework process unreadable through ``/proc`` to a same-uid peer.

A framework process in the cron container resolves credentials, so the decrypted
values live in its heap, reachable through ``/proc/<pid>/mem`` by a same-uid peer.
(The credential store itself no longer travels in the environment, and no longer
crosses an ``execve``: ``docker/cron/drop.py`` reads it as root, drops privilege,
calls this, and only then loads it — see ``framework/store_env.py`` and
docs/architecture/cron-privileges.md.)

An agent runs as the SAME uid, and a same-uid ptrace-mode read needs only that
the target is *dumpable*. ``PR_SET_DUMPABLE=0`` withdraws that: the kernel
reparents the process's ``/proc`` entries to root and denies ptrace-mode access,
and because these containers drop ``CAP_SYS_PTRACE`` nothing in the container can
override it.

What this does and does not do:

* **Closes** the heap channel — a peer can no longer read a decrypted SSH key or
  provider credential out of a framework process's memory (DECISIONS.md D-SSH-1
  residual channel (4)).
* **Closes** ``/proc/<pid>/fd`` and ptrace-mode access to a same-uid peer. In the
  store-bearing path ``drop.py`` calls this *between* the privilege drop and the
  load, so the payload is never held by a dumpable process.
* Does NOT create a boundary between agent_views — one uid, one ``/workspace``.
  The store capability is withheld by the file's ownership and the launcher
  (D-SSH-1 residual channel (6), closed 2026-09-23), not by this call.

This is process hardening, not a boundary between agent_views.

Stated limits: ``execve`` resets dumpable to 1, so a process that execs and then
calls this has a short window — which is exactly why the store is loaded after the
drop and never handed across an exec; and a parent shell that exported the
variables still holds them in its own heap (its exec-time environ does not carry
them).
Side effects: no core dumps for framework processes, and the process cannot read
its own ``/proc/self/fd``, so CPython's descriptor closing falls back from the
``/proc`` scan to ``close_range``/a brute-force loop.
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
