"""Make a framework process unreadable through ``/proc`` to a same-uid peer.

Every framework process started in the cron container gets the credential-store
environment (``MYSQL_*``, ``AGENTO_ENCRYPTION_KEY``, ``CONFIG__*``) because the
crontab and the consumer are launched by ``su - agent -c "source
/opt/cron-agent/env; …"``. That environment reaches the Python process as its
exec-time environ, so ``/proc/<pid>/environ`` carries the passphrase — and the
decrypted credentials themselves live in that process's heap, reachable through
``/proc/<pid>/mem``.

An agent runs as the SAME uid, and a same-uid ptrace-mode read needs only that
the target is *dumpable*. ``PR_SET_DUMPABLE=0`` withdraws that: the kernel
reparents the process's ``/proc`` entries to root and denies ptrace-mode access,
and because these containers drop ``CAP_SYS_PTRACE`` nothing in the container can
override it.

What this does and does not do:

* **Closes** the heap channel — a peer can no longer read a decrypted SSH key or
  provider credential out of a framework process's memory (DECISIONS.md D-SSH-1
  residual channel (4)).
* **Removes one route** to the credential store: ``/proc/<pid>/environ``.
* **Does NOT** reduce the store capability itself. ``/opt/cron-agent/env`` is
  mode 0644 and any agent-uid process can still source it and decrypt every
  stored credential. That is D-SSH-1 residual channel (6), owner-accepted
  2026-08-25 until AG-42 (Option B) — closing it needs the env file to go away,
  which needs a launcher that keeps the secrets out of an agent-readable place.

This is process hardening, not a boundary between agent_views.

Stated limits: ``execve`` resets dumpable to 1, so there is a short window
between exec and this call; and a parent shell that exported the variables still
holds them in its own heap (its exec-time environ does not carry them).
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
