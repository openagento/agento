"""The runner socket's wire format: one JSON object per line, both ways. The payload is the
existing dataclasses; ``encode`` writes an enum as its value and a datetime as ISO text,
and the ``*_from_wire`` builders read them back."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from enum import Enum

from ..agent_manager.models import CredentialRecord, CredentialStatus
from ..harness.runtime import HarnessRunContext, McpInitReport, McpServerStatus, RunResult

MAX_REQUEST = 1 << 20     # one request line (the spec carries the prompt)
MAX_FRAME = 256 << 10     # one event line
CHUNK_CHARS = 16 << 10    # a JSON-escaped char is at most 12 bytes (a surrogate pair): 12 x 16 Ki < MAX_FRAME
MAX_OUTPUT = 4 << 20      # the joined text of one field, the retention cap of the run
_DATETIMES = ("expires_at", "used_at", "created_at", "updated_at", "throttled_until",
              "leased_until", "limits_at")


def _default(obj):
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def encode(obj) -> bytes:
    """One line. ``asdict`` keeps a ``repr=False`` field: the line is a secret in transit,
    and it is never logged (SEC-6)."""
    return json.dumps(obj, default=_default, separators=(",", ":")).encode() + b"\n"


def context_from_wire(d: dict) -> HarnessRunContext:
    cred = d.get("credential")
    if cred is not None:
        cred = {**cred, "status": CredentialStatus(cred["status"])}
        for name in _DATETIMES:
            if cred.get(name) is not None:
                cred[name] = datetime.fromisoformat(cred[name])
        cred = CredentialRecord(**cred)
    return HarnessRunContext(**{**d, "credential": cred})


def result_from_wire(d: dict) -> RunResult:
    mcp = d.get("mcp_init")
    if mcp is not None:
        mcp = McpInitReport(servers=tuple(McpServerStatus(**s) for s in mcp["servers"]))
    return RunResult(**{**d, "mcp_init": mcp})
