"""The credential store, held in-process and never in ``os.environ``.

The store is the container's credential-store capability: the database credentials
(``MYSQL_*``), the passphrase every stored credential is decrypted with
(``AGENTO_ENCRYPTION_KEY``) and any ``CONFIG__*`` override. ``docker/cron/drop.py``
reads ``/opt/cron-agent/env`` while it is still root, drops to uid ``agent`` in-process
and calls :func:`load`; this module parses it into a private mapping. The store crosses
no ``execve``, so it is never exposed by the dumpable window an exec reopens.

Why not ``os.environ``: a value placed there is copied into every later child at
``execve`` and then sits in that child's ``/proc/<pid>/environ``, which any same-uid
process can read for the child's whole life. The framework builds child environments
from ``{**os.environ, …}`` in several places, so a single ``os.environ.update`` here
would leak the store to all of them. Keeping the values out of ``os.environ`` means
those sites inherit a mapping the store was never added to — no per-site edit, and
no further place to forget.

Serialization: NUL-delimited ``KEY=value`` records, each split at the FIRST ``=``.
An environment value may legitimately contain a newline, so newline-delimited records
would let a password split into a forged second assignment. ``/opt/cron-agent/env.public``
uses the same format, read by the launcher's bash loop.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from types import MappingProxyType

_store: dict[str, str] = {}


def parse(raw: bytes) -> dict[str, str]:
    """NUL-delimited ``KEY=value`` records → mapping. Raises on a malformed record.

    Fail closed: a partial parse would leave the process running with half a
    configuration and the delivery defect invisible.
    """
    parsed: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue  # trailing delimiter
        text = record.decode("utf-8", errors="surrogateescape")
        name, sep, value = text.partition("=")
        if not sep or not name:
            raise ValueError(f"credential store: malformed record (no '=' in {len(record)} bytes)")
        parsed[name] = value
    return parsed


def load(raw: bytes) -> None:
    """Install ``raw`` as the store. Raises on a malformed payload (fail closed)."""
    _store.update(parse(raw))


def get(name: str, default: str | None = None) -> str | None:
    """The store first, then the ambient environment."""
    if name in _store:
        return _store[name]
    return os.environ.get(name, default)


def environ() -> Mapping[str, str]:
    """A read-only merged view, for callers that scan by prefix (``CONFIG__*``)."""
    return MappingProxyType({**os.environ, **_store})


def reset() -> None:
    """Drop the loaded store. For tests."""
    _store.clear()
