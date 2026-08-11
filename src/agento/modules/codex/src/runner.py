from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta

from agento.framework.agent_manager.errors import (
    AuthenticationError,
    TransientAuthError,
    UsageLimitError,
)
from agento.framework.harness import RunResult, SubprocessRunner

# Anchored auth phrases checked ONLY against turn.failed.error.message in the
# NDJSON stream. Never matched against raw stdout/stderr — that's the bug we're
# fixing (substring "401" in MCP payload order numbers used to poison tokens).
_AUTH_PHRASE_RE = re.compile(
    r"\b(401\s+Unauthorized|invalid[_ ]api[_ ]key|"
    r"please\s+(sign|log)\s+in|not\s+authenticated|"
    r"authentication\s+failed|missing\s+bearer)\b",
    re.IGNORECASE,
)

# Auth rejections that do NOT prove the credential is dead (revoked/stale access
# token, typically a concurrent-refresh race). Same structured-event-only discipline
# as _AUTH_PHRASE_RE, and the same two-signal discipline as the Claude parser:
# revocation wording must co-occur with credential context, otherwise
# "workspace access has been revoked" would throttle a perfectly good token.
# These THROTTLE + fail over (TransientAuthError) rather than poison, and are
# checked AFTER _AUTH_PHRASE_RE so codex's known-permanent phrases are unaffected.
_REVOKED_RE = re.compile(r"revok", re.IGNORECASE)
_CREDENTIAL_WORD_RE = re.compile(
    r"oauth|access token|credential|bearer|api key|token[_ ]revoked",
    re.IGNORECASE,
)

# Session/usage/rate-limit phrases — checked ONLY against turn.failed.error.message
# (same anti-false-positive discipline as auth). These map to a TEMPORARY throttle
# (fail over + auto-recover), distinct from the permanent auth poison above.
_LIMIT_PHRASE_RE = re.compile(
    r"\b(429|too\s+many\s+requests|rate[_ ]limit(?:_exceeded|_error)?|"
    r"usage[_ ]limit(?:_reached)?|quota\s+exceeded|insufficient_quota)\b",
    re.IGNORECASE,
)

# Best-effort reset hint in a codex limit message, e.g. "try again in 90s",
# "retry after 3600 seconds", "try again in 1h2m3s". Returns None if absent.
_RETRY_AFTER_RE = re.compile(
    r"(?:try\s+again\s+in|retry(?:[-\s]after)?)\s+"
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s(?:econds?)?)?",
    re.IGNORECASE,
)


class CodexSubprocessRunner(SubprocessRunner):
    """Runs the Codex CLI. Commands come from CodexCommandBuilder."""

    def _credential_env(self, credential: object | None) -> dict[str, str]:
        if credential is None:
            return {}
        from agento.modules.codex.src.config import CodexWorkspaceAdapter
        return CodexWorkspaceAdapter().credential_env(credential)

    def _try_parse_session_id(self, line: str) -> str | None:
        """Streaming hook: extract thread_id from the first ``thread.started``
        event so the consumer can resume even if the process is killed."""
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(ev, dict) and ev.get("type") == "thread.started":
            return ev.get("thread_id") or None
        return None

    def _extract_raw(self, proc: subprocess.CompletedProcess) -> str:
        """Codex --json emits NDJSON on stdout. Stderr (Rust tracing output)
        is deliberately ignored — it can contain '401' substrings from log
        lines that would false-positive substring-based scans. The base
        runner still preserves stderr in its own logs."""
        return proc.stdout or ""

    def _parse_output(self, raw: str) -> RunResult:
        """Parse codex exec --json NDJSON stdout.

        Raises ``AuthenticationError`` only when a structured ``turn.failed``
        event carries an anchored auth phrase. Any other failure path
        (non-zero exit, missing turn.completed, malformed lines) returns a
        best-effort ``RunResult`` and lets the consumer retry without
        poisoning the token.
        """
        events = _parse_ndjson(raw)

        if (msg := _detect_auth_error(events)) is not None:
            raise AuthenticationError(f"Codex CLI auth error: {msg[:500]}")

        if (msg := _detect_transient_auth_error(events)) is not None:
            raise TransientAuthError(f"Codex CLI transient auth error: {msg[:500]}")

        if (err := _detect_limit_error(events)) is not None:
            msg = str(err.get("message") or err.get("type") or err.get("code") or "usage limit")
            raise UsageLimitError(
                f"Codex CLI error: {msg[:500]}", reset_at=_reset_at_from_error(err)
            )

        result = RunResult(raw_output=_extract_agent_text(events))
        _populate_session(events, result)
        _populate_usage(events, result)
        _populate_mcp_init(events, result)
        return result


def _parse_ndjson(raw: str) -> list[dict]:
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _detect_auth_error(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") != "turn.failed":
            continue
        err = ev.get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else ""
        if msg and _AUTH_PHRASE_RE.search(msg):
            return msg
    return None


def _detect_transient_auth_error(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") != "turn.failed":
            continue
        err = ev.get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else ""
        if msg and _REVOKED_RE.search(msg) and _CREDENTIAL_WORD_RE.search(msg):
            return msg
    return None


def _detect_limit_error(events: list[dict]) -> dict | None:
    """Return the ``turn.failed`` error dict on a session/usage/rate limit, else None.

    Like ``_detect_auth_error`` it inspects ONLY the structured ``turn.failed.error``
    object — never raw stdout — so a "429" substring inside an MCP payload can't
    false-positive. Beyond the human ``message`` it also matches the machine-readable
    ``type``/``code`` fields, so a typed limit error with a bland (or empty) message is
    still classified (a codex ``turn.failed`` can exit rc=0 — missing it would
    dead-letter as a false SUCCESS)."""
    for ev in events:
        if ev.get("type") != "turn.failed":
            continue
        err = ev.get("error")
        if not isinstance(err, dict):
            continue
        haystack = " ".join(
            str(err.get(k, "")) for k in ("message", "type", "code")
        )
        if haystack.strip() and _LIMIT_PHRASE_RE.search(haystack):
            return err
    return None


def _reset_at_from_error(err: dict, now: datetime | None = None) -> datetime | None:
    """Derive a naive-UTC reset time from a codex limit error. Prefers machine-readable
    fields (``retry_after``/``retry_after_ms``/``reset_after_seconds`` as a delay, or
    ``reset_at``/``reset`` as an epoch), then falls back to a "try again in …" hint in
    the message text. Returns ``None`` when nothing is parseable (the consumer then
    applies its default throttle window). ``now`` is injectable for tests. Never raises."""
    base = now or datetime.now(UTC)
    # Delay-in-seconds fields.
    for key in ("retry_after", "retry_after_seconds", "reset_after_seconds"):
        v = err.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return (base + timedelta(seconds=int(v))).astimezone(UTC).replace(tzinfo=None)
    # Delay-in-milliseconds.
    v = err.get("retry_after_ms")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return (base + timedelta(milliseconds=int(v))).astimezone(UTC).replace(tzinfo=None)
    # Absolute epoch-seconds reset.
    for key in ("reset_at", "reset"):
        v = err.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            try:
                return datetime.fromtimestamp(int(v), tz=UTC).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                pass
    return _parse_reset_at(str(err.get("message") or ""), now=now)


def _parse_reset_at(msg: str, now: datetime | None = None) -> datetime | None:
    """Best-effort: derive a naive-UTC reset time from a codex limit message's
    "try again in …" / "retry after …" hint. Returns ``None`` when absent. ``now`` is
    injectable for deterministic tests. Never raises."""
    m = _RETRY_AFTER_RE.search(msg or "")
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        return None
    base = now or datetime.now(UTC)
    return (base + timedelta(seconds=total)).astimezone(UTC).replace(tzinfo=None)


def _extract_agent_text(events: list[dict]) -> str:
    parts: list[str] = []
    for ev in events:
        if ev.get("type") != "item.completed":
            continue
        item = ev.get("item") or {}
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _populate_session(events: list[dict], result: RunResult) -> None:
    for ev in events:
        if ev.get("type") == "thread.started":
            thread_id = ev.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                result.session_id = thread_id
            return


def _populate_usage(events: list[dict], result: RunResult) -> None:
    for ev in events:
        if ev.get("type") != "turn.completed":
            continue
        usage = ev.get("usage") or {}
        if not isinstance(usage, dict):
            return
        in_tok = usage.get("input_tokens")
        if isinstance(in_tok, int):
            result.input_tokens = in_tok
        out_tok = usage.get("output_tokens")
        reason_tok = usage.get("reasoning_output_tokens")
        total_out = 0
        if isinstance(out_tok, int):
            total_out += out_tok
        if isinstance(reason_tok, int):
            total_out += reason_tok
        if total_out:
            result.output_tokens = total_out
        return


def _populate_mcp_init(events: list[dict], result: RunResult) -> None:
    """Scan codex NDJSON events for a session-level MCP-server init self-report.

    Empirical finding (codex 0.128.0, verified against a real production session
    captured in ``tests/fixtures/codex/real_success_with_mcp.ndjson`` — see the
    note in ``app_monitor/README.md``): ``codex exec --json`` emits NO startup
    event listing MCP servers and their connection status. The only event types
    observed are ``thread.started``, ``turn.{started,completed,failed}``,
    ``item.{started,completed}`` (with ``item.type`` in {``agent_message``,
    ``command_execution``, ``mcp_tool_call``}), and ``error``. MCP only ever
    surfaces as per-tool-call ``mcp_tool_call`` items — those report that a tool
    was *invoked*, NOT whether the server *connected* at session start.

    There is therefore no init self-report to capture, and ``result.mcp_init``
    is intentionally left as ``None`` ("we don't know"). Deliberately we do NOT
    infer connection status from ``mcp_tool_call`` items: that would conflate
    "tool was used" (the ``toolbox_mcp_calls`` count, derived independently from
    the transcript reader) with "server connected at init".

    This helper is kept (rather than omitted) so that a future codex version
    which *does* emit a structured init report has one obvious place to wire it
    in — at which point add a fixture under ``tests/fixtures/codex/`` and a
    matching test. Inventing an init schema before codex ships one is out of
    scope.
    """
    return None
