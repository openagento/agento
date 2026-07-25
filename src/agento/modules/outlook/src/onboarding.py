from __future__ import annotations

import logging

import pymysql

# Identity + auth live at the DEFAULT scope (the global Azure app). Auth is satisfied by EITHER a
# client secret OR a certificate PEM (its contents, stored encrypted) — checked separately. The
# mailbox UPN may live at the default scope (single-view) OR an agent_view scope (multi-view), so it
# is checked at ANY scope, not just default.
_IDENTITY_KEYS = (
    "outlook/outlook_tenant_id",
    "outlook/outlook_client_id",
)
_AUTH_KEYS = (
    "outlook/outlook_client_secret",
    "outlook/outlook_cert_pem",
)
_MAILBOX_KEY = "outlook/outlook_mailbox_user_id"
_RESTRICT_READ_KEY = "outlook/restrict_read_to_allowed_senders"
_FALSY_VALUES = {"0", "false", "no", "off"}

_PEM_END_SENTINEL = "END"


def _conflicting_mailbox_scopes(conn, mailbox, target_scope, target_scope_id):
    """Return ``[(scope, scope_id)]`` rows where ``outlook_mailbox_user_id`` already holds the SAME
    normalized mailbox UPN at a scope OTHER than the write target.

    The mailbox resolves ``agent_view -> workspace -> default``, so a duplicate at ANY scope means two
    agent_views resolve to the same inbox. That is now a supported configuration: the publisher polls
    the shared mailbox ONCE and routes each message to a view by matching the sender against
    ``outlook_sender`` ingress bindings (routed mode). This is surfaced during onboarding so the
    operator sets up the bindings. Comparison is on the normalized UPN (strip + lowercase — the same
    key the publisher/cursor use); the exact target row (same ``scope`` + ``scope_id``) is excluded so
    re-running onboarding for the same view is not a self-conflict. Module-level for unit-testability.
    """
    normalized = (mailbox or "").strip().lower()
    if not normalized:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope, scope_id FROM core_config_data "
            "WHERE path = %s AND value IS NOT NULL AND LOWER(TRIM(value)) = %s "
            "AND NOT (scope = %s AND scope_id = %s)",
            (_MAILBOX_KEY, normalized, target_scope, target_scope_id),
        )
        rows = cur.fetchall()
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append((row["scope"], row["scope_id"]))
        else:
            result.append((row[0], row[1]))
    return result


def _read_gate_disabled_scopes(conn):
    """Return ``[(scope, scope_id)]`` where ``restrict_read_to_allowed_senders`` is explicitly set to a
    falsy value (the read gate is disabled — agent can read unauthenticated mail). Default (unset) is
    secure (``true``), so only explicit falsy rows are reported. Module-level for unit-testability."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope, scope_id, value FROM core_config_data WHERE path = %s",
            (_RESTRICT_READ_KEY,),
        )
        rows = cur.fetchall()
    result = []
    for row in rows:
        if isinstance(row, dict):
            scope, sid, val = row["scope"], row["scope_id"], row["value"]
        else:
            scope, sid, val = row[0], row[1], row[2]
        if val is not None and str(val).strip().lower() in _FALSY_VALUES:
            result.append((scope, sid))
    return result


def _warn_read_gate_disabled(conn) -> None:
    """Print a warning if the read gate is disabled at any scope. Called on every onboarding run so an
    operator re-configuring Outlook is reminded that reading is unrestricted somewhere."""
    scopes = _read_gate_disabled_scopes(conn)
    if not scopes:
        return
    where = ", ".join(f"{s}:{sid}" for s, sid in scopes)
    print(
        f"\n  WARNING: outlook/restrict_read_to_allowed_senders is DISABLED at scope(s) {where}.\n"
        "    The agent read tools can then read ANY message in the mailbox — including spoofed,\n"
        "    non-allow-listed, or DMARC-failed mail (a prompt-injection vector). It is scoped config\n"
        "    (agent_view > workspace > default), not a global switch. Re-enable per scope with:\n"
        "      agento config:set outlook/restrict_read_to_allowed_senders 1"
        "   # add --scope agent_view --scope-id <id> to target a view"
    )


def _read_pem_block(prompt: str) -> str:
    """Read a multi-line pasted PEM from stdin until a lone ``END`` line (or EOF).

    Module-level so it is unit-testable by mocking ``input``.
    """
    print(prompt)
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == _PEM_END_SENTINEL:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _pem_has_cert_and_key(pem: str) -> bool:
    """A usable Azure app PEM must contain BOTH a certificate block AND a private-key block.

    Azure's ``ClientCertificatePEMCertificate`` requires both; a cert-only paste otherwise fails
    opaquely at Graph token acquisition. ``PRIVATE KEY-----`` covers PRIVATE KEY / RSA PRIVATE KEY /
    ENCRYPTED PRIVATE KEY.
    """
    return "-----BEGIN CERTIFICATE-----" in pem and "PRIVATE KEY-----" in pem


def _print_next_steps() -> None:
    """Print the operator's post-onboarding checklist (tools opt-in; allow-list; polling opt-in).

    Printed on EVERY onboarding run — including when Graph verification is skipped (no toolbox URL) —
    so the operator always sees the enable steps. Module-level for unit-testability.
    """
    print(
        "\n  Next steps (Outlook tools are opt-in; senders are allow-listed; polling is opt-in):\n"
        "    1) Enable the channel tools (one tool_name per `tool:enable` call):\n"
        "       for t in outlook_get_message outlook_reply outlook_mark_processed; do \\\n"
        "         agento tool:enable \"$t\" --agent-view <code>; done\n"
        "    2) Allow-list the authorized sender(s) (per-view: add --scope agent_view --scope-id <id>):\n"
        "       agento config:set outlook/allowed_senders \"<sender-address>[,<more>]\"\n"
        "    3) Enable polling for the mailbox's scope (default is off, so the publisher skips it):\n"
        "       agento config:set outlook/enabled 1   # per-view: add --scope agent_view --scope-id <id>\n"
        "    4) Recommended (least privilege): restrict the Azure app to THIS mailbox only so a leaked\n"
        "       toolbox credential can't read/send tenant-wide — see docs/modules/outlook.md\n"
        "       'Least privilege: scope the app to one mailbox'."
    )


class OutlookOnboarding:
    def describe(self) -> str:
        return "Configure Outlook / Microsoft 365 mailbox connection (Graph app credentials)"

    def is_complete(self, conn: pymysql.connections.Connection) -> bool:
        # Identity + auth (the global Azure app) must exist at the DEFAULT scope. The mailbox UPN may
        # live at ANY scope: default (single-view) or agent_view (multi-view).
        default_keys = _IDENTITY_KEYS + _AUTH_KEYS
        placeholders = ",".join(["%s"] * len(default_keys))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT path FROM core_config_data "
                f"WHERE path IN ({placeholders}) AND scope = 'default' AND scope_id = 0 "
                f"AND value IS NOT NULL AND value <> ''",
                default_keys,
            )
            found = {row["path"] if isinstance(row, dict) else row[0] for row in cur.fetchall()}
            has_identity = set(_IDENTITY_KEYS).issubset(found)
            has_auth = any(k in found for k in _AUTH_KEYS)
            cur.execute(
                "SELECT 1 FROM core_config_data "
                "WHERE path = %s AND value IS NOT NULL AND value <> '' LIMIT 1",
                (_MAILBOX_KEY,),
            )
            has_mailbox = cur.fetchone() is not None
        return has_identity and has_auth and has_mailbox

    def run(self, conn, config: dict, logger: logging.Logger) -> None:
        import getpass

        from agento.framework.cli import terminal
        from agento.framework.core_config import (
            config_delete,
            config_set,
            config_set_auto_encrypt,
        )
        from agento.framework.scoped_config import Scope, scoped_config_set
        from agento.framework.workspace import get_active_agent_views

        print("\n=== Outlook / Microsoft 365 onboarding ===")
        tenant = input("Azure tenant ID: ").strip()
        client_id = input("Azure app (client) ID: ").strip()
        mailbox = input("Mailbox UPN to monitor (e.g. agent@example.com): ").strip()

        auth_choice = terminal.select(
            "Graph authentication method",
            ["Client secret (stored encrypted)", "Certificate (paste PEM contents)"],
        )
        config_set(conn, "outlook/outlook_tenant_id", tenant)
        config_set(conn, "outlook/outlook_client_id", client_id)
        # All credential writes AND the stale-branch deletes below run in the SAME transaction as the
        # single conn.commit() at the end — never after it. The Graph verification reads config via the
        # toolbox's own DB connection (committed rows only); deleting after the commit would let stale
        # cert material survive (graph-auth gives the certificate precedence when both are present).
        if auth_choice == 0:
            secret = getpass.getpass("Azure app client secret: ").strip()
            config_set_auto_encrypt(conn, "outlook/outlook_client_secret", secret)
            # Switched to secret auth: drop any stale certificate material (+ legacy path).
            config_delete(conn, "outlook/outlook_cert_pem")
            config_delete(conn, "outlook/outlook_cert_password")
            config_delete(conn, "outlook/outlook_cert_path")
        else:
            pem = ""
            while True:
                pem = _read_pem_block(
                    "Paste the certificate PEM (cert + private key), then a line with just END:"
                )
                if not pem:
                    # Same reason as the mailbox-conflict abort below: discard the uncommitted
                    # tenant/client writes so an aborted run cannot read back as complete.
                    conn.rollback()
                    print("  Aborted: no PEM provided (nothing saved).")
                    return
                if _pem_has_cert_and_key(pem):
                    break
                print(
                    "  Error: the PEM must contain BOTH a certificate block and a private-key block. "
                    "Paste the full PEM (cert + key) again."
                )
            # NOTE: do NOT .strip() the passphrase — surrounding whitespace can be significant. Only an
            # exactly-empty string means "no passphrase".
            cert_password = getpass.getpass(
                "Certificate PEM passphrase (leave empty if unencrypted): "
            )
            config_set_auto_encrypt(conn, "outlook/outlook_cert_pem", pem)
            if cert_password != "":
                config_set_auto_encrypt(conn, "outlook/outlook_cert_password", cert_password)
            else:
                config_delete(conn, "outlook/outlook_cert_password")
            # Switched to certificate auth: drop any stale client secret (+ legacy path).
            config_delete(conn, "outlook/outlook_client_secret")
            config_delete(conn, "outlook/outlook_cert_path")

        # Mailbox: the mailbox identifies the agent_view. One mailbox per onboarding run.
        verify_agent_view_id: int | None = None
        views = get_active_agent_views(conn)
        chosen_av = None
        if len(views) > 1:
            idx = terminal.select(
                "Which agent_view owns this mailbox?", [av.code for av in views]
            )
            chosen_av = views[idx]
            target_scope, target_scope_id = Scope.AGENT_VIEW, chosen_av.id
        else:
            # 0 or 1 active view -> default scope (a single view resolves it via fallback).
            target_scope, target_scope_id = Scope.DEFAULT, 0

        # Shared-mailbox notice: the mailbox resolves agent_view -> workspace -> default, so the same
        # UPN at ANY other scope means two views share one inbox. This is now ROUTED mode — the inbox
        # is polled once and each message is routed to a view by sender. Bind senders with:
        #   agento ingress:bind outlook_sender '<regex>' <view> --priority <n>
        # Warn + confirm; never hard-block (a deliberate shared mailbox is legitimate).
        conflicts = _conflicting_mailbox_scopes(conn, mailbox, target_scope, target_scope_id)
        if conflicts:
            where = ", ".join(f"{s}:{sid}" for s, sid in conflicts)
            print(f"  NOTE: mailbox '{mailbox}' is already configured at scope(s) {where}.")
            print("  Two agent_views resolving to the same mailbox share one inbox — it is polled once")
            print("  and each message is routed to a view by sender (routed mode). Configure bindings:")
            print("    agento ingress:bind outlook_sender '<regex>' <view> --priority <n>")
            print("  (see docs/modules/outlook.md). Unmatched senders are not published.")
            if terminal.select(
                "Proceed and set this mailbox anyway?",
                ["No — abort (nothing saved)", "Yes — set it anyway"],
            ) == 0:
                # Roll back the uncommitted identity/auth writes from this run — otherwise setup's
                # is_complete(conn) sees them on the same connection (plus the pre-existing mailbox row
                # that triggered this conflict) and treats the aborted run as complete, later committing
                # the partial state. Rollback makes "nothing saved" truthful.
                conn.rollback()
                print("  Aborted: nothing saved (no config written).")
                return

        if chosen_av is not None:
            scoped_config_set(
                conn, _MAILBOX_KEY, mailbox,
                scope=Scope.AGENT_VIEW, scope_id=chosen_av.id, encrypted=False,
            )
            verify_agent_view_id = chosen_av.id
            print(f"  Mailbox '{mailbox}' bound to agent_view '{chosen_av.code}'.")
        else:
            config_set(conn, _MAILBOX_KEY, mailbox)
        conn.commit()

        # Read-gate warning (scoped config, not a global switch): remind the operator if reading is
        # unrestricted anywhere — shown on every run, including when Graph verification is skipped below.
        _warn_read_gate_disabled(conn)

        # Verify Graph auth + mailbox access NOW (the toolbox reads the just-committed config per
        # request and decrypts the obscure secret, so /api/outlook/delta exercises the real creds).
        from agento.framework.bootstrap import get_module_config
        from agento.modules.outlook.src.toolbox_client import (
            OutlookToolboxClient,
            ToolboxAPIError,
        )

        core_cfg = get_module_config("core")
        toolbox_url = core_cfg.get("toolbox/url", "") if isinstance(core_cfg, dict) else ""
        if not toolbox_url:
            print("  Saved, but core/toolbox/url is not set — cannot verify now. "
                  "Set it, then run `agento outlook:publish --top 1`.")
            _print_next_steps()
            return
        client = OutlookToolboxClient(toolbox_url)
        try:
            client.list_delta(top=1, agent_view_id=verify_agent_view_id)
            logger.info("Outlook Graph verification OK")
            print(f"  Verified: Graph auth + mailbox '{mailbox}' reachable.")
        except ToolboxAPIError as e:
            print(f"  Error: Graph verification failed ({e}). Check tenant/client/auth/mailbox.")
        except Exception as e:  # toolbox unreachable, network, etc.
            print(f"  Error: Toolbox not reachable at {toolbox_url}: {e}")
        finally:
            client.close()

        # Tools ship DISABLED (opt-in), an allow-list gate is required, and polling is opt-in — tell
        # the operator the explicit steps before Outlook acts.
        _print_next_steps()
