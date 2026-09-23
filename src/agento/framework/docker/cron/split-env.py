#!/usr/bin/env python3
"""Partition the container environment into the store file and the public file.

Reads ``env -0`` on stdin (NUL-delimited, so a value containing a newline survives)
and writes two NUL-delimited files:

* ``/opt/cron-agent/env``        — every name that grants the credential store,
  ``root:root 0600``. It reaches a framework process only on a file descriptor
  the launcher opens; no uid-``agent`` path can read it.
* ``/opt/cron-agent/env.public`` — the rest of the persisted whitelist, which the
  launcher imports into the dropped process's environment.

The split predicate is :func:`agento.framework.credential_store_env.is_credential_store_name`,
the same one that strips the store from an agent spawn — so a secret added later cannot
quietly land on the public side.
"""
from __future__ import annotations

import os
import sys

from agento.framework.credential_store_env import is_credential_store_name

STORE_FILE = "/opt/cron-agent/env"
PUBLIC_FILE = "/opt/cron-agent/env.public"

# Names that survive the privilege drop at all. Anything outside this set is dropped:
# see docs/architecture/cron-env-contract.md (the AGENTO_* prefix promise).
PERSISTED_PREFIXES = ("MYSQL_", "CONFIG__", "AGENTO_")
PERSISTED_NAMES = frozenset({"TZ", "PYTHONPATH", "PROVIDER", "DISABLE_LLM", "DISABLE_AUTOUPDATER"})


def partition(raw: bytes) -> tuple[bytes, bytes]:
    store: list[bytes] = []
    public: list[bytes] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        name = record.split(b"=", 1)[0].decode("utf-8", errors="replace")
        if not (name.startswith(PERSISTED_PREFIXES) or name in PERSISTED_NAMES):
            continue
        (store if is_credential_store_name(name) else public).append(record)
    return (
        b"".join(r + b"\0" for r in store),
        b"".join(r + b"\0" for r in public),
    )


def main() -> int:
    store, public = partition(sys.stdin.buffer.read())
    # 0600 before a single byte lands in it. The open mode applies only when the file is
    # CREATED, so an existing file — one a pre-V0 image left at 0644 — keeps its old mode
    # through O_TRUNC; fchmod is what covers the upgrade.
    fd = os.open(STORE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(store)
    with open(PUBLIC_FILE, "wb") as fh:
        fh.write(public)
    os.chmod(PUBLIC_FILE, 0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main())
