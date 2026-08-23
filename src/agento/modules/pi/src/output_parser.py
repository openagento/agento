"""Parse Pi's ``--mode json`` NDJSON stream.

Two disciplines this module exists to enforce, both learned the hard way by the
other harnesses:

**Only stdout is parsed.** Pi writes NDJSON to stdout and diagnostics to stderr. A
substring scan over stderr is how "401" inside an unrelated log line used to poison a
healthy credential (see ``modules/codex/src/runner.py``). Stderr stays a diagnostic
channel, preserved for ``job.output``, and is inspected only for the one narrow
model-mismatch phrase in ``runner.py`` — which yields an ordinary run error, never a
credential verdict.

**Credential state is decided only from a provider-attributable field.** For Pi that is
the ``errorMessage`` of an assistant message whose ``stopReason`` is ``"error"``. Tool
results are Toolbox output — Jira comments, mail bodies, SQL rows — i.e. text that the
person filing a ticket controls. An order number "401" in a Jira comment must never mark
a credential dead.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

TOOLBOX_INIT_RECORD = "agento-toolbox-init"
MODEL_MISMATCH_RECORD = "agento-model-mismatch"

# Anchored phrases, matched ONLY against an assistant error message. Never against raw
# stdout/stderr and never against tool results.
_AUTH_PHRASE_RE = re.compile(
    r"\b(401\s+unauthorized|invalid[_ ]api[_ ]key|no\s+auth\s+credentials|"
    r"authentication\s+failed|missing\s+bearer|user\s+not\s+found)\b",
    re.IGNORECASE,
)

# Quota / rate limiting -> throttle + fail over, distinct from the permanent poison above.
_LIMIT_PHRASE_RE = re.compile(
    r"\b(429\s+too\s+many\s+requests|rate[_ ]limit(ed|)|quota\s+exceeded|"
    r"insufficient\s+credits|402\s+payment\s+required|payment\s+required)\b",
    re.IGNORECASE,
)

# Network-level failure reaching the provider: the credential is probably fine.
_TRANSIENT_RE = re.compile(
    r"\b(econnreset|econnrefused|etimedout|enotfound|socket\s+hang\s+up|"
    r"network\s+error|fetch\s+failed|502\s+bad\s+gateway|503\s+service\s+unavailable)\b",
    re.IGNORECASE,
)


@dataclass
class ParsedStream:
    """Everything the runner needs from one Pi NDJSON stream."""

    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    num_turns: int = 0
    text: str = ""
    provider: str | None = None
    model: str | None = None
    error_message: str | None = None
    mcp_init_raw: dict | None = None
    model_mismatch: dict | None = None
    # Every (provider, model) pair seen on an assistant message, in order. Keeping only
    # the last would hide a mid-run switch.
    identities: list[tuple[str | None, str | None]] = field(default_factory=list)


def iter_events(raw: str):
    """Yield the JSON objects in an NDJSON stream, skipping anything unparseable.

    Pi can interleave non-JSON warnings; a single bad line must not lose the run.
    """
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def parse_session_id(line: str) -> str | None:
    """Streaming hook: the header is the FIRST line of a ``--mode json`` run.

    ``{"type":"session","version":3,"id":"…","timestamp":"…","cwd":"…"}`` — emitted
    before any prompt is sent, so the consumer can record the session even if the
    process is later killed. ``-p`` does not emit it; only ``--mode json`` does.
    """
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(event, dict) and event.get("type") == "session":
        session_id = event.get("id")
        return str(session_id) if session_id else None
    return None


def _assistant_error(message: dict) -> str | None:
    """The one channel allowed to decide credential state."""
    if message.get("role") != "assistant":
        return None
    if message.get("stopReason") != "error":
        return None
    err = message.get("errorMessage")
    return err if isinstance(err, str) and err else None


def parse_stream(raw: str) -> ParsedStream:
    """Fold an NDJSON stream into the fields the runner reports."""
    out = ParsedStream()
    texts: list[str] = []

    for event in iter_events(raw):
        etype = event.get("type")

        if etype == "session" and out.session_id is None:
            session_id = event.get("id")
            if session_id:
                out.session_id = str(session_id)

        elif etype == "turn_end":
            # Deliberately Pi's own notion of a turn, which is NOT comparable with
            # claude's or codex's num_turns. Recorded in DECISIONS.md.
            out.num_turns += 1

        elif etype == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                continue

            usage = message.get("usage")
            if isinstance(usage, dict):
                out.input_tokens += int(usage.get("input") or 0)
                out.output_tokens += int(usage.get("output") or 0)

            # docs/session-format.md:85-86,209 — assistant messages carry these.
            # Used to prove the model that actually ran is the one requested; Pi
            # resolves an unmatched model by silent substring matching.
            if message.get("role") == "assistant":
                provider = message.get("provider")
                model = message.get("model")
                provider = provider if isinstance(provider, str) else None
                model = model if isinstance(model, str) else None
                out.identities.append((provider, model))
                if provider:
                    out.provider = provider
                if model:
                    out.model = model

            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        texts.append(text)

            if out.error_message is None:
                out.error_message = _assistant_error(message)

        elif etype == "entry_appended":
            # `pi.appendEntry(customType, data)` surfaces on stdout as
            # {"type":"entry_appended","entry":{"customType":…,"data":…}}.
            entry = event.get("entry")
            if not isinstance(entry, dict):
                continue
            if entry.get("customType") == TOOLBOX_INIT_RECORD:
                data = entry.get("data")
                if isinstance(data, dict):
                    out.mcp_init_raw = data
            elif entry.get("customType") == MODEL_MISMATCH_RECORD:
                data = entry.get("data")
                if isinstance(data, dict):
                    out.model_mismatch = data

    out.text = "\n".join(texts)
    return out


def classify_error(message: str | None):
    """Map an assistant error message to an exception class, or ``None``.

    Order matters: a limit message that happens to mention 401 keeps its limit
    classification (throttle + fail over) rather than poisoning the credential.
    """
    from agento.framework.agent_manager.errors import (
        AuthenticationError,
        TransientAuthError,
        UsageLimitError,
    )

    if not message:
        return None
    if _LIMIT_PHRASE_RE.search(message):
        return UsageLimitError(f"Pi CLI error: {message[:500]}")
    if _AUTH_PHRASE_RE.search(message):
        return AuthenticationError(f"Pi CLI error: {message[:500]}")
    if _TRANSIENT_RE.search(message):
        return TransientAuthError(f"Pi CLI error: {message[:500]}")
    return None
