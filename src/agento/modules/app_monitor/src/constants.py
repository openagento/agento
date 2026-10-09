"""Config keys + domain constants for app_monitor.

Constants are field-relative — they index the dict returned by
``get_module_config("app_monitor")``. See ``system.json`` for the schema and
``config.json`` for the defaults.
"""
from __future__ import annotations

# --- config keys (field-relative, no module prefix) ---

CFG_SEND_ALERT_ON_MCP_ISSUES = "send_alert_on_mcp_issues"

CFG_ALERT_EMAIL_TO       = "alerts/email_to"
CFG_ALERT_SMTP_HOST      = "alerts/smtp_host"
CFG_ALERT_SMTP_PORT      = "alerts/smtp_port"
CFG_ALERT_SMTP_USER      = "alerts/smtp_user"
CFG_ALERT_SMTP_PASSWORD  = "alerts/smtp_password"
CFG_ALERT_SMTP_FROM      = "alerts/smtp_from"
CFG_ALERT_SMTP_TLS       = "alerts/smtp_tls"

# --- MCP init status vocabulary (the CLI's own words, an open string on the wire) ---

MCP_STATUS_CONNECTED = "connected"

# Statuses meaning the CLI decided this server will not serve tools this session.
MCP_STATUS_NOT_CONNECTED = frozenset({
    "failed", "needs-auth", "needs-approval", "disabled",
})

# Recognized indeterminate statuses: the connect simply had not finished when the
# CLI printed its init line. Expected on every job whenever the connect is
# non-blocking, so this resolves to UNKNOWN (NULL) *silently* — never to "not
# connected", never with a warning. Three sets, not two, so that "expected
# indeterminate" stays distinguishable from "a word we have never seen".
MCP_STATUS_TRANSIENT = frozenset({"pending"})
