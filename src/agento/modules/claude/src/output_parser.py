from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agento.framework.agent_manager.errors import (
    AuthenticationError,
    TransientAuthError,
    UsageLimitError,
)
from agento.framework.harness import McpInitReport, McpServerStatus, RunResult

AUTH_ERROR_PHRASES = (
    "authentication_error",
    "OAuth token has expired",
    "Not logged in",
    "401 Invalid authentication credentials",
)

# Session/usage/rate-limit phrases. These are TEMPORARY throttles (fail over + auto-recover),
# distinct from AUTH_ERROR_PHRASES (permanent poison). Matched case-insensitively.
LIMIT_ERROR_PHRASES = (
    "hit your session limit",
    "usage limit",
    "usage_limit_reached",
    "rate_limit_error",
)

# A transient credential rejection needs BOTH a credential-context word AND a rejection
# signal. Either signal alone is too weak to act on:
#   * credential word alone  -> "the access token could not be read from disk" (a local
#                               file problem; throttling the pool token fixes nothing)
#   * rejection signal alone -> "workspace access has been revoked" / "permission has
#                               been revoked" (an authorization failure inside the run,
#                               not a bad pool credential), or "401234 tokens".
# Both regexes scan the WHOLE message, so the rule is order-independent: the observed
# wording puts the auth verb BEFORE the code ("Failed to authenticate. API Error:
# 401 ..."), future wording may not.
# \b401\b, not "401" as a substring — a substring test matches "401234 tokens".
# Bare "token" is deliberately absent from the credential words — Claude says "tokens"
# about usage accounting constantly ("max tokens", "input tokens").
_CREDENTIAL_WORD_RE = re.compile(
    r"authenticat|unauthorized|oauth|access token|credential|bearer|api key|"
    r"sign in|signed in|log in|logged in",
    re.IGNORECASE,
)
_401_CODE_RE = re.compile(r"\b401\b")
_REVOKED_RE = re.compile(r"revok", re.IGNORECASE)  # revoked / revoke / revocation

# "resets 1pm (Europe/Warsaw)" / "resets 1:30am (America/New_York)" — capture clock + tz.
_RESET_RE = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\s*\(([^)]+)\)",
    re.IGNORECASE,
)


# Backward-compatible alias — all existing imports of ClaudeResult keep working.
ClaudeResult = RunResult

__all__ = [
    "AUTH_ERROR_PHRASES",
    "LIMIT_ERROR_PHRASES",
    "AuthenticationError",
    "ClaudeResult",
    "TransientAuthError",
    "UsageLimitError",
    "parse_claude_output",
]


def _parse_reset_at(msg: str, now: datetime | None = None) -> datetime | None:
    """Parse a Claude limit message's reset time (e.g. ``resets 1pm (Europe/Warsaw)``)
    into a naive-UTC datetime. Returns the next occurrence of that wall-clock time in
    the named IANA timezone (rolling to tomorrow if it has already passed today).
    ``now`` is injectable for deterministic tests (defaults to ``datetime.now(UTC)``).
    Returns ``None`` on anything unparseable — never raises."""
    m = _RESET_RE.search(msg or "")
    if not m:
        return None
    hour_s, minute_s, ampm, tz_s = m.groups()
    try:
        tz = ZoneInfo(tz_s.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    try:
        hour = int(hour_s)
        minute = int(minute_s) if minute_s else 0
    except ValueError:
        return None
    if ampm:
        ampm = ampm.lower()
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base = (now or datetime.now(UTC)).astimezone(tz)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= base:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC).replace(tzinfo=None)


def _is_transient_credential_rejection(msg: str) -> bool:
    if not _CREDENTIAL_WORD_RE.search(msg):
        return False
    return bool(_401_CODE_RE.search(msg) or _REVOKED_RE.search(msg))


def _classify_error(msg: str):
    """Return the exception to raise for a Claude ``is_error`` result message.

    Order matters:
    1. Known-permanent auth phrases -> AuthenticationError (poison the token).
    2. Limit phrases -> UsageLimitError (throttle to reset_at + fail over); checked
       before the transient rule so a limit message carrying a 401 keeps its parsed
       ``reset_at``.
    3. Credential word + (401 | revoked) -> TransientAuthError (short throttle + fail
       over, no poison). Covers the reported revoked-token message AND future 401
       wording.
    4. Anything else -> RuntimeError (generic retry).
    """
    if any(p in msg for p in AUTH_ERROR_PHRASES):
        return AuthenticationError(f"Claude CLI error: {msg}")
    low = msg.lower()
    if any(p in low for p in LIMIT_ERROR_PHRASES):
        return UsageLimitError(f"Claude CLI error: {msg}", reset_at=_parse_reset_at(msg))
    if _is_transient_credential_rejection(msg):
        return TransientAuthError(f"Claude CLI error: {msg}")
    return RuntimeError(f"Claude CLI error: {msg}")


def parse_claude_output(raw: str, logger: logging.Logger | None = None) -> RunResult:
    """Parse Claude CLI stream-json (JSONL) output into a RunResult.

    Falls back to single-JSON parsing for backward compatibility.
    """
    _log = logger or logging.getLogger(__name__)

    # Backward compat: single JSON object (old --output-format json)
    stripped = raw.strip()
    if stripped.startswith("{") and "\n" not in stripped:
        try:
            data = json.loads(stripped)
            # Old format has "result", "is_error", or "usage" at top level
            # Stream-json events have "type" as their discriminator
            if isinstance(data, dict) and "type" not in data:
                return _parse_single_json(raw, _log)
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse JSONL events line by line
    session_id: str | None = None
    result_event: dict | None = None
    mcp_init: McpInitReport | None = None
    init_seen = False

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(event, dict):
            continue

        if event.get("session_id"):
            session_id = event["session_id"]

        # First `system/init` event wins; ignore any subsequent ones.
        if (
            not init_seen
            and event.get("type") == "system"
            and event.get("subtype") == "init"
            and isinstance(event.get("mcp_servers"), list)
        ):
            init_seen = True
            mcp_init = _extract_mcp_init(event["mcp_servers"])
            if mcp_init is not None:
                # Log only sanitized name/status pairs — never the raw event,
                # which can carry prompts, tool arguments, and customer data.
                _log.debug(
                    "mcp init: %s",
                    [(s.name, s.status) for s in mcp_init.servers],
                )

        if event.get("type") == "result":
            result_event = event

    if result_event:
        if result_event.get("is_error"):
            msg = result_event.get("result", "unknown error")
            raise _classify_error(msg)

        usage = result_event.get("usage", {})
        return RunResult(
            raw_output=raw,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost_usd=result_event.get("total_cost_usd"),
            num_turns=result_event.get("num_turns"),
            duration_ms=result_event.get("duration_ms"),
            session_id=session_id or result_event.get("session_id"),
            mcp_init=mcp_init,
        )

    # No result event (partial output from timeout) — return what we have
    if session_id:
        return RunResult(raw_output=raw, session_id=session_id, mcp_init=mcp_init)

    # Length only: `raw` is the agent's output and can carry prompt echoes or
    # customer data. The full text is persisted to `job.output`.
    _log.warning(
        f"No result event in stream-json output ({len(raw)}b; "
        f"see job.output)"
    )
    return RunResult(raw_output=raw, mcp_init=mcp_init)


def _extract_mcp_init(servers: list) -> McpInitReport | None:
    """Build an McpInitReport from a claude ``system/init`` event's mcp_servers.

    An empty list yields ``McpInitReport(servers=())`` — a valid "no MCP servers
    visible" report, distinct from ``None`` ("no init report at all"). A malformed
    entry (not a dict, or missing string name/status) makes the whole report
    untrustworthy → return ``None`` and never raise.
    """
    parsed: list[McpServerStatus] = []
    for entry in servers:
        if not isinstance(entry, dict):
            return None
        name = entry.get("name")
        status = entry.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            return None
        parsed.append(McpServerStatus(name=name, status=status))
    return McpInitReport(servers=tuple(parsed))


def _parse_single_json(raw: str, _log: logging.Logger) -> RunResult:
    """Parse Claude CLI single JSON output into a RunResult (legacy format)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        _log.warning(
            f"Claude output is not valid JSON ({exc}); {len(raw)}b "
            f"(see job.output)"
        )
        return RunResult(raw_output=raw)

    if data.get("is_error"):
        msg = data.get("result", "unknown error")
        raise _classify_error(msg)

    cr = RunResult(
        raw_output=raw,
        input_tokens=data.get("usage", {}).get("input_tokens"),
        output_tokens=data.get("usage", {}).get("output_tokens"),
        cost_usd=data.get("total_cost_usd"),
        num_turns=data.get("num_turns"),
        duration_ms=data.get("duration_ms"),
        # The legacy payload's own ``subtype`` is an outcome word ("success"), never a
        # session id — reading it into ``session_id`` would make a resume target garbage.
        session_id=data.get("session_id"),
    )
    if cr.num_turns is None and cr.input_tokens is None:
        _log.warning(f"Claude JSON has no usage data, keys={list(data.keys())}")
    return cr
