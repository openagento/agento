"""Event observers for app_monitor.

- ``McpHealthTelemetryObserver`` (``job_finalize_before``) — pure telemetry.
  Records two independent, nullable per-attempt signals on the ``job`` row and
  (optionally) emails ops when something looks off. It does NOT set a verdict
  and never disrupts job flow: an rc=0 job stays a SUCCESS. The two signals:
    * ``toolbox_mcp_calls``  — count of ``mcp__toolbox__*`` tool-uses seen in the
      on-disk transcript. ``0`` = parsed, none found. ``NULL`` = unknown
      (no reader, missing/unreadable transcript, or parser drift).
    * ``toolbox_mcp_connected`` — what the CLI self-reported for ``toolbox`` in
      its session-init line, mapped tri-state: ``TRUE`` only for
      ``connected``; ``FALSE`` for a status the CLI treats as terminal
      (``failed``/``needs-auth``/…) or when ``toolbox`` is missing from the
      report; ``NULL`` ("we don't know") when there is no init report at all
      **or** the status is merely indeterminate — notably ``pending``, which
      only means the handshake had not finished when init was printed.
  The transcript parser lives in the agent's module (claude/codex/…); this
  observer resolves one via ``get_transcript_reader(provider)`` so the
  framework — and this module — stay agent-agnostic.
- ``AlertEmailObserver`` (``job_dead_after``) — send a plain-text SMTP alert
  on DEAD transitions when both ``alerts/email_to`` and ``alerts/smtp_host``
  are configured. Silent no-op if either is empty; SMTP failures are logged
  but never propagated.
- ``SecurityBreachAlertObserver`` (``security_breach_after``) — send a
  plain-text SMTP alert when an inbound channel reports a probable security
  breach (e.g. a spoofed sender). Same fail-quiet contract as
  ``AlertEmailObserver``.
"""
from __future__ import annotations

import logging

from agento.framework.bootstrap import get_module_config
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.events import JobVerificationFailed
from agento.framework.transcript_reader import get_transcript_reader

from .constants import (
    CFG_ALERT_EMAIL_TO,
    CFG_ALERT_SMTP_FROM,
    CFG_ALERT_SMTP_HOST,
    CFG_ALERT_SMTP_PASSWORD,
    CFG_ALERT_SMTP_PORT,
    CFG_ALERT_SMTP_TLS,
    CFG_ALERT_SMTP_USER,
    CFG_SEND_ALERT_ON_MCP_ISSUES,
    MCP_STATUS_CONNECTED,
    MCP_STATUS_NOT_CONNECTED,
    MCP_STATUS_TRANSIENT,
    MCP_TOOLBOX_TOOL_PREFIX,
    PARSE_DRIFT_MIN_LINES,
)
from .emailer import SmtpConfig, send_alert

logger = logging.getLogger(__name__)

_MODULE_NAME = "app_monitor"

_TOOLBOX_SERVER_NAME = "toolbox"


def _config() -> dict:
    return get_module_config(_MODULE_NAME) or {}


def _flag(key: str) -> bool:
    """Resolve a boolean config flag. Missing key → False; string values
    ``"true"/"yes"/"1"/"on"`` (case-insensitive) → True; anything else → False."""
    raw = _config().get(key)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _save_mcp_telemetry(job_id: int, calls: int | None, connected: bool | None) -> None:
    """Persist both per-attempt MCP signals on the ``job`` row in one UPDATE.

    Always rewrites BOTH columns — including to NULL — because job rows are
    reused across retry attempts (retry-with-fresh-session writes the same row).
    Skipping the write on unknown signals would leave stale values from a prior
    attempt (e.g. attempt 1's ``3/TRUE`` surviving an attempt 2 that couldn't
    read the transcript). Best-effort: a DB hiccup logs a warning and returns;
    it never crashes the observer or changes job flow. PyMySQL maps Python
    ``True``→1, ``False``→0, ``None``→NULL for the BOOLEAN column.
    """
    try:
        conn = get_connection(DatabaseConfig.from_env())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job SET toolbox_mcp_calls = %s, "
                    "toolbox_mcp_connected = %s, updated_at = NOW() WHERE id = %s",
                    (calls, connected, job_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "Failed to persist MCP telemetry (calls=%r, connected=%r) for "
            "job_id=%s (best-effort)",
            calls, connected, job_id, exc_info=True,
        )


class McpHealthTelemetryObserver:
    """Record MCP-health telemetry for a finalizing job — no verdict, ever.

    Resolves the provider-specific transcript reader via the framework
    registry — never imports a provider-specific module directly. Both signals
    are independently nullable; missing data is ``NULL``, never coerced to
    ``0`` / ``False``.
    """

    def execute(self, event) -> None:
        # Whole-body backstop: telemetry is best-effort and must never disrupt
        # job flow. Sub-steps already guard their own failures; this catch
        # guarantees the observer never raises even if config access throws.
        try:
            job = getattr(event, "job", None)
            job_id = getattr(job, "id", None)
            provider = getattr(event, "provider", None)
            session_id = getattr(job, "session_id", None) if job is not None else None

            toolbox_calls = self._count_toolbox_calls(provider, session_id, job_id)
            toolbox_connected, toolbox_status = self._resolve_toolbox_status(event)

            if isinstance(job_id, int) and job_id > 0:
                _save_mcp_telemetry(job_id, toolbox_calls, toolbox_connected)

            # Separate from the parse log in _count_toolbox_calls (which runs
            # before the status is resolved): the raw status word is otherwise
            # only visible at DEBUG, inside the provider's output parser.
            logger.info(
                "McpHealthTelemetryObserver: mcp health",
                extra={
                    "job_id": job_id,
                    "provider": provider,
                    "session_id": session_id,
                    "toolbox_mcp_calls": toolbox_calls,
                    "toolbox_mcp_connected": toolbox_connected,
                    "toolbox_status": toolbox_status,
                },
            )

            self._maybe_alert(
                job, provider, session_id, toolbox_calls, toolbox_connected, toolbox_status,
            )
        except Exception:
            logger.warning(
                "McpHealthTelemetryObserver: unexpected error (best-effort, "
                "job_id=%s)", getattr(getattr(event, "job", None), "id", "?"),
                exc_info=True,
            )
        # event.verdict is intentionally never touched — telemetry only.

    def _count_toolbox_calls(self, provider, session_id, job_id) -> int | None:
        """Count ``mcp__toolbox__*`` tool-uses in the on-disk transcript.

        Returns ``None`` whenever the count is unknown (no session id, no reader
        for the provider, missing/unreadable transcript, or parser drift); ``0``
        only when the transcript parsed cleanly and held zero toolbox calls.
        """
        if not session_id:
            return None
        reader = get_transcript_reader(provider) if provider else None
        if reader is None:
            logger.info(
                "McpHealthTelemetryObserver: no TranscriptReader for provider=%r "
                "— toolbox call count unknown (job_id=%s)", provider, job_id,
            )
            return None
        try:
            summary = reader.parse(session_id)
        except FileNotFoundError:
            return None  # transcript missing — count unknown
        except Exception:
            logger.exception(
                "McpHealthTelemetryObserver: error reading transcript for "
                "provider=%s session_id=%s — toolbox count unknown",
                provider, session_id,
            )
            return None

        # Drift detection (log-only): a non-trivial transcript whose records the
        # reader didn't recognize at all is almost certainly a silent provider
        # format change. Log at ERROR so ops can spot it, but leave the count
        # NULL (the measurement is unreliable) and set no verdict.
        if (
            summary.total_json_lines >= PARSE_DRIFT_MIN_LINES
            and summary.recognized_records == 0
        ):
            logger.error(
                "parser drift detected: provider=%s session_id=%s — %d JSON "
                "lines, 0 recognized records",
                provider, session_id, summary.total_json_lines,
            )
            return None

        toolbox_calls = sum(
            1 for t in summary.tool_uses if t.name.startswith(MCP_TOOLBOX_TOOL_PREFIX)
        )
        logger.info(
            "McpHealthTelemetryObserver: parsed transcript",
            extra={
                "job_id": job_id,
                "provider": provider,
                "session_id": session_id,
                "toolbox_mcp_calls": toolbox_calls,
                "tool_uses_total": len(summary.tool_uses),
                "recognized_records": summary.recognized_records,
                "json_lines_total": summary.total_json_lines,
            },
        )
        return toolbox_calls

    def _resolve_toolbox_status(self, event) -> tuple[bool | None, str]:
        """Resolve ``toolbox_mcp_connected`` + the raw status word behind it.

        The status is the CLI's own vocabulary and an open string on the wire, so
        an unrecognized word resolves to "unknown", never to "not connected" — a
        renamed status must not read as an outage.

        * ``(None, "no-init-report")`` — no init report at all (provider lacks the
          capability, or the stream had no init event). Distinct from ``False``.
        * ``(True, status)``  — ``toolbox`` is listed ``connected``.
        * ``(False, status)`` — ``toolbox`` is listed with a status the CLI treats
          as terminal for this session (``MCP_STATUS_NOT_CONNECTED``).
        * ``(None, status)``  — ``toolbox`` is listed but the status is merely
          indeterminate: ``MCP_STATUS_TRANSIENT`` (``pending`` — the handshake had
          not finished when init was printed) resolves silently; anything
          unrecognized resolves the same way but logs a warning.
        * ``(False, "absent")`` / ``(False, "no-servers")`` — an init report exists
          but ``toolbox`` is not in it (the empty list is a valid report saying "no
          MCP servers visible"). "init present, toolbox not visible" is FALSE.
        """
        job_result = getattr(event, "job_result", None)
        mcp_init = getattr(job_result, "mcp_init", None) if job_result is not None else None
        if mcp_init is None:
            return None, "no-init-report"
        for server in mcp_init.servers:
            if server.name == _TOOLBOX_SERVER_NAME:
                if server.status == MCP_STATUS_CONNECTED:
                    return True, server.status
                if server.status in MCP_STATUS_NOT_CONNECTED:
                    return False, server.status
                if server.status not in MCP_STATUS_TRANSIENT:
                    logger.warning(
                        "McpHealthTelemetryObserver: unrecognized MCP status %r for "
                        "server %r — recording UNKNOWN; extend MCP_STATUS_* if this "
                        "is a real state", server.status, _TOOLBOX_SERVER_NAME,
                    )
                return None, server.status
        return False, ("absent" if mcp_init.servers else "no-servers")

    def _maybe_alert(self, job, provider, session_id, calls, connected, status) -> None:
        """Send one combined alert per attempt when the flag is on, SMTP is
        configured, and at least one explicit-bad signal is present. NULL signals
        ("unknown") never trigger — only ``calls == 0`` or ``connected is False``.

        ``status`` is the raw init status word; it goes into the subject and body
        so an alert says *why* rather than a bare ``Connected: False``.
        """
        if not _flag(CFG_SEND_ALERT_ON_MCP_ISSUES):
            return
        zero_calls = (calls == 0)            # explicit 0, not falsy — NULL must NOT trigger
        not_connected = (connected is False)  # identity check — NULL must NOT trigger
        if not (zero_calls or not_connected):
            return

        smtp = _smtp_config()
        to = (_config().get(CFG_ALERT_EMAIL_TO) or "").strip()
        if smtp is None or not to:
            return  # not configured — silent no-op

        conditions = []
        if zero_calls:
            conditions.append("0 toolbox calls")
        if not_connected:
            conditions.append(f"toolbox not connected ({status})")
        matched = " + ".join(conditions)

        job_id = getattr(job, "id", "?")
        subject = f"[agento] Job {job_id} MCP health — {matched}"
        body = "\n".join([
            f"Job id:        {job_id}",
            f"Source:        {getattr(job, 'source', '?')}",
            f"Reference id:  {getattr(job, 'reference_id', '?')}",
            f"Provider:      {provider}",
            f"Session id:    {session_id}",
            f"Attempt:       {getattr(job, 'attempt', '?')}/{getattr(job, 'max_attempts', '?')}",
            f"Toolbox calls: {calls}",
            f"Connected:     {connected}",
            f"Toolbox status: {status}",
            f"Matched:       {matched}",
        ])
        try:
            send_alert(smtp, to, subject, body)
        except Exception:
            logger.warning(
                "McpHealthTelemetryObserver: SMTP send failed (job_id=%s)",
                job_id, exc_info=True,
            )


def _smtp_config() -> SmtpConfig | None:
    cfg = _config()
    host = (cfg.get(CFG_ALERT_SMTP_HOST) or "").strip()
    if not host:
        return None
    return SmtpConfig(
        host=host,
        port=int(cfg.get(CFG_ALERT_SMTP_PORT) or 587),
        user=(cfg.get(CFG_ALERT_SMTP_USER) or ""),
        password=(cfg.get(CFG_ALERT_SMTP_PASSWORD) or ""),
        from_addr=(cfg.get(CFG_ALERT_SMTP_FROM) or ""),
        tls=bool(cfg.get(CFG_ALERT_SMTP_TLS, True)),
    )


def _format_body(event) -> tuple[str, str]:
    """Compose a short subject/body for the DEAD alert.

    When the underlying error is a verification veto, surface
    ``verdict.reason`` and ``verdict.detail`` so ops can immediately tell a
    parser-drift dead-letter from an agent-fault dead-letter without opening
    a transcript.
    """
    job = event.job
    err = event.error
    err_class = err.__class__.__name__
    subject = f"[agento] Job {job.id} DEAD — {err_class}"
    lines = [
        f"Job id:       {job.id}",
        f"Reference id: {job.reference_id}",
        f"Source:       {job.source}",
        f"Attempt:      {job.attempt}/{job.max_attempts}",
        f"Error class:  {err_class}",
        f"Error:        {str(err)[:1000]}",
        f"Elapsed ms:   {event.elapsed_ms}",
    ]
    if isinstance(err, JobVerificationFailed):
        lines.append(f"Verdict reason: {err.verdict.reason.value}")
        if err.verdict.detail:
            lines.append(f"Verdict detail: {err.verdict.detail}")
    return subject, "\n".join(lines)


class AlertEmailObserver:
    """Send a plain-text alert on DEAD-letter transitions."""

    def execute(self, event) -> None:
        cfg = _config()
        to = (cfg.get(CFG_ALERT_EMAIL_TO) or "").strip()
        smtp = _smtp_config()
        if not to or smtp is None:
            return  # not configured — silent no-op
        subject, body = _format_body(event)
        try:
            send_alert(smtp, to, subject, body)
        except Exception:
            logger.warning(
                "AlertEmailObserver: SMTP send failed (job_id=%s)",
                getattr(event.job, "id", "?"),
                exc_info=True,
            )


class SecurityBreachAlertObserver:
    """Send a plain-text alert when an inbound channel reports a probable security breach.

    Same fail-quiet contract as ``AlertEmailObserver``: silent no-op unless both
    ``alerts/email_to`` and ``alerts/smtp_host`` are configured; SMTP failures are logged but
    never propagated (the dispatcher also swallows observer errors).
    """

    def execute(self, event) -> None:
        cfg = _config()
        to = (cfg.get(CFG_ALERT_EMAIL_TO) or "").strip()
        smtp = _smtp_config()
        if not to or smtp is None:
            return  # not configured — silent no-op
        channel = getattr(event, "channel", "?")
        reason = getattr(event, "reason", "?")
        subject = f"[agento] Security breach attempt — {channel} ({reason})"
        body = "\n".join([
            f"Channel:      {channel}",
            f"Reason:       {reason}",
            f"Sender:       {getattr(event, 'sender', '?')}",
            f"Reference id: {getattr(event, 'reference_id', '?')}",
            f"Detail:       {getattr(event, 'detail', '') or ''}",
        ])
        try:
            send_alert(smtp, to, subject, body)
        except Exception:
            logger.warning(
                "SecurityBreachAlertObserver: SMTP send failed (channel=%s)",
                channel, exc_info=True,
            )


# Human-readable expansion of MailboxStalledEvent.reason codes, so the ops email is actionable
# without opening the source. An unknown code still sends (generic line) — never a silent drop.
_STALL_REASONS = {
    "policy_divergence": "Shared-mailbox members disagree on activation-policy config "
                         "(allowed_senders is per-view and does not stall); the group is NOT "
                         "polled until the config is reconciled.",
    "no_bindings": "Routed mode but no active outlook_sender bindings; ALL mail to this mailbox is "
                   "being dropped (configure `agento ingress:bind outlook_sender ...`).",
    "upn_mismatch": "Configured mailbox UPN disagrees with the resolved mailbox; the poll is held "
                    "(no delivery) until the drift is resolved.",
}


class MailboxStalledAlertObserver:
    """Send a plain-text alert when an inbound channel's shared mailbox stops delivering mail due to
    a misconfiguration (divergent policy / no bindings / UPN mismatch).

    Same fail-quiet contract as ``AlertEmailObserver``: silent no-op unless both ``alerts/email_to``
    and ``alerts/smtp_host`` are configured; SMTP failures are logged but never propagated (the
    dispatcher also swallows observer errors).
    """

    def execute(self, event) -> None:
        cfg = _config()
        to = (cfg.get(CFG_ALERT_EMAIL_TO) or "").strip()
        smtp = _smtp_config()
        if not to or smtp is None:
            return  # not configured — silent no-op
        channel = getattr(event, "channel", "?")
        mailbox = getattr(event, "mailbox", "?")
        reason = getattr(event, "reason", "?")
        meaning = _STALL_REASONS.get(reason, "This mailbox is not delivering mail.")
        subject = f"[agento] Mailbox not delivering — {mailbox} ({reason})"
        body = "\n".join([
            f"Channel:      {channel}",
            f"Mailbox:      {mailbox}",
            f"Reason:       {reason}",
            f"Meaning:      {meaning}",
            f"Detail:       {getattr(event, 'detail', '') or ''}",
        ])
        try:
            send_alert(smtp, to, subject, body)
        except Exception:
            logger.warning(
                "MailboxStalledAlertObserver: SMTP send failed (channel=%s mailbox=%s)",
                channel, mailbox, exc_info=True,
            )
