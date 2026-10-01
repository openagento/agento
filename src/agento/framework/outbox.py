"""The framework's transactional outbox (PRD E3-E5 §6.4.1).

`write_outbox` takes the CALLER's cursor. That is the whole design: the row commits in the
transaction that made the transition it describes, so a claimed job with no event, or an
event for a claim that rolled back, cannot exist. A writer with its own connection would
be a second transaction and neither guarantee would hold.

The table is module-agnostic on purpose (PLC-2): no `conversation_id`, no module name. A
relay resolves whatever a module needs from `job_id`.
"""
from __future__ import annotations

import json
import re

KIND_GRAMMAR = re.compile(r"^[a-z][a-z0-9_.]{0,31}$")

# A payload is read back as JSON by a relay that fans it out. Anything that is not plain
# data cannot survive that round trip, so it is refused at the write, where the traceback
# still names the caller.
_PRIMITIVES = (str, int, float, bool, type(None))

# A config path or an API key in a payload is a secret one relay away from a browser. This
# is a guard against a mistake, not a classifier: it refuses the shapes that would be a
# leak, and a caller that needs a value like these puts an identifier in the payload
# instead and lets the reader resolve it.
_FORBIDDEN_KEY = re.compile(
    r"(^|/)(smtp_pass|password|secret|token|credential)|^CONFIG__|^sk-[a-z]{2,}-|"
    r"(^|/)[a-z0-9_]+/(smtp_pass|password|secret|token)",
    re.IGNORECASE,
)

# A VALUE is judged by shape, not by vocabulary. The key check above refuses a field NAMED
# `password`, which is where a leak actually arrives; refusing any value that merely
# contains the word refuses ordinary English - `credential_error`, §6.4.2's own failure
# kind, is the case that found this. What a secret looks like is a config path or a vendor
# key prefix, and those are exactly what stays here.
_FORBIDDEN_VALUE = re.compile(
    r"^CONFIG__|^sk-[a-z]{2,}-|(^|/)[a-z0-9_]+/(smtp_pass|password|secret|token)",
    re.IGNORECASE,
)


class PayloadError(ValueError):
    """A refused outbox write. Never a warning: a bad row is read by something else later,
    where the caller that wrote it is no longer in the traceback."""


def _check_plain(value: object, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PayloadError(f"{path}: a key must be a string, got {key!r}")
            if _FORBIDDEN_KEY.search(key):
                raise PayloadError(f"{path}: key {key!r} looks like configuration or a secret")
            _check_plain(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_plain(item, f"{path}[{i}]")
        return
    if not isinstance(value, _PRIMITIVES):
        raise PayloadError(f"{path}: {type(value).__name__} is not JSON data")
    if isinstance(value, str) and _FORBIDDEN_VALUE.search(value):
        raise PayloadError(f"{path}: the value looks like configuration or a secret")


def write_outbox(cursor, *, job_id: int, kind: str, payload: dict,
                 execution_id: str | None = None) -> int:
    """Write one outbox row through the caller's cursor. Returns its id.

    The only writer of `job_event_outbox`.
    """
    if not isinstance(kind, str) or not KIND_GRAMMAR.match(kind):
        raise PayloadError(
            f"kind {kind!r} is invalid - lowercase letters, digits, '_' and '.', "
            "starting with a letter, at most 32 characters"
        )
    if not isinstance(payload, dict):
        raise PayloadError(f"payload must be an object, got {type(payload).__name__}")
    _check_plain(payload)

    cursor.execute(
        "INSERT INTO job_event_outbox (job_id, execution_id, kind, payload) "
        "VALUES (%s, %s, %s, %s)",
        (job_id, execution_id, kind, json.dumps(payload)),
    )
    return cursor.lastrowid


def prune_outbox(conn, *, retention_days: int | None = None) -> int:
    """Delete every row older than `core/outbox/retention_days`, **relayed or not**.

    Relayed-only would be the unbounded case: with the consuming module disabled nothing is
    ever relayed, so every framework row would live for ever (CODE-8). A module's own,
    shorter retention over relayed rows is a faster cleanup inside this backstop, never a
    replacement for it.
    """
    days = retention_days if retention_days is not None else _configured_retention(conn)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM job_event_outbox WHERE created_at < NOW() - INTERVAL %s DAY", (days,)
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted


def _configured_retention(conn) -> int:
    from pathlib import Path

    from .bootstrap import CORE_MODULES_DIR
    from .config_resolver import load_db_overrides, read_config_defaults, resolve_field

    core = Path(CORE_MODULES_DIR) / "core"
    schema = json.loads((core / "system.json").read_text())["outbox/retention_days"]
    return int(resolve_field("core", "outbox/retention_days", schema,
                             read_config_defaults(core), load_db_overrides(conn)).value)
