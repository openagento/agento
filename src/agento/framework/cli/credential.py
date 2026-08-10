from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import UTC, datetime

from ..db import get_connection_or_exit
from ..log import get_logger
from .runtime import _load_framework_config


def _mask(secret: str) -> str:
    """Return secret with all but first 4 + last 4 chars replaced by '*'.
    Secrets of length <= 8 are fully masked."""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def _read_secret(prompt: str) -> str:
    """Read a one-line secret from stdin.

    TTY → ``getpass.getpass`` (echo suppressed).
    Pipe/redirect → one line from ``sys.stdin``.
    Exits with an error if the result is empty after stripping."""
    raw = getpass.getpass(prompt) if sys.stdin.isatty() else sys.stdin.readline()
    secret = raw.strip()
    if not secret:
        print("No secret provided on stdin.", file=sys.stderr)
        sys.exit(1)
    return secret


class CredentialRegisterCommand:
    @property
    def name(self) -> str:
        return "credential:register"

    @property
    def shortcut(self) -> str:
        return "cr:reg"

    @property
    def help(self) -> str:
        return "Register a new credential (stored encrypted in DB)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("scope", help="Credential scope (see `agento credential:list`)")
        parser.add_argument("label")
        parser.add_argument("--token-limit", type=int, default=0, dest="token_limit")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--with-api-key", dest="with_api_key", action="store_true",
            help="Read provider API key from stdin (piped/redirected) or "
                 "interactive prompt (input hidden). Codex: OpenAI key. "
                 "Claude: Anthropic key.",
        )
        mode.add_argument(
            "--with-access-token", dest="with_access_token", action="store_true",
            help="Read access-token JWT from stdin or interactive prompt "
                 "(Codex only). Skips interactive OAuth.",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import CredentialLeasedError, register_credential
        from ..events import CredentialRegisteredEvent, dispatch_credential_event

        db_config, _, _ = _load_framework_config()
        logger = get_logger("agent-manager")

        scope = _validate_scope(args.scope)
        credentials, token_type = _resolve_credentials(args, scope, logger)

        conn = get_connection_or_exit(db_config)
        try:
            try:
                credential = register_credential(
                    conn,
                    scope=scope,
                    label=args.label,
                    credentials=credentials,
                    token_limit=args.token_limit,
                    type=token_type,
                    logger=logger,
                )
            except CredentialLeasedError as exc:
                # A duplicate label is an upsert, so this can refuse here too. Roll back so
                # no partial transaction survives, and refuse rather than wait: an
                # interactive OAuth round-trip is already spent by now, and blocking a TTY
                # for minutes is worse than telling the operator to retry.
                conn.rollback()
                print(f"Refusing to overwrite a credential in use: {exc}", file=sys.stderr)
                sys.exit(1)
            conn.commit()
            print(f"Registered credential: id={credential.id} label={credential.label} type={credential.type}")
        finally:
            conn.close()

        dispatch_credential_event(
            "credential_register_after",
            CredentialRegisteredEvent(
                scope=scope,
                credential_id=credential.id,
                label=credential.label,
                credentials=credentials,
                type=credential.type,
            ),
        )


def _validate_scope(scope: str) -> str:
    """Validate a credential scope against the harness registry (no hardcoded list)."""
    from ..harness import list_credential_scopes

    known = list_credential_scopes()
    if scope not in known:
        print(
            f"Error: unknown credential scope {scope!r}. Available: {known or '(none registered)'}",
            file=sys.stderr,
        )
        sys.exit(1)
    return scope


def _supported_modes(scope: str) -> set:
    """Registration modes DECLARED for this credential scope (no hasattr probing)."""
    from ..harness import get_harness_for_scope

    registered = get_harness_for_scope(scope)
    return {
        m
        for p in (registered.descriptor.providers if registered else ())
        if p.credential_scope == scope
        for m in p.registration_modes
    }


def _require_mode(scope: str, mode, what: str) -> None:
    """Exit with the supported-mode list unless ``mode`` is declared for ``scope``.

    A scope with no registered owner yields no modes — that means "unknown", not
    "unsupported" (the CLI tolerates a failed bootstrap when the DB is down). Absence of
    information must not masquerade as a definitive refusal, so the check is skipped;
    ``_validate_scope`` already rejects a genuinely unknown scope with a better message.
    """
    from ..harness import get_harness_for_scope

    if get_harness_for_scope(scope) is None:
        return
    supported = _supported_modes(scope)
    if mode not in supported:
        print(
            f"Error: {what} is not supported for scope {scope!r}. "
            f"Supported modes: {sorted(m.value for m in supported)}",
            file=sys.stderr,
        )
        sys.exit(1)


def _resolve_credentials(args: argparse.Namespace, scope: str, logger) -> tuple[dict, str]:
    """Resolve registration input to (credentials_dict, type)."""
    from ..agent_manager.auth import AuthenticationError, authenticate_interactive
    from ..harness import (
        CredentialRegistrationMode,
        get_authenticator,
    )

    # 1. Explicit credential flags (api_key / access_token)
    if args.with_access_token or args.with_api_key:
        authenticator = get_authenticator(scope)
        if authenticator is None:
            print(f"Error: no authenticator registered for scope {scope!r}", file=sys.stderr)
            sys.exit(1)

        mode = (
            CredentialRegistrationMode.ACCESS_TOKEN
            if args.with_access_token else CredentialRegistrationMode.API_KEY
        )
        flag = "--with-access-token" if args.with_access_token else "--with-api-key"
        _require_mode(scope, mode, flag)

        label = mode.value.replace("_", " ")
        secret = _read_secret(
            f"Paste {label} for {scope} (input hidden, press Enter when done): "
        )
        # Masked echo on stderr so operators can sanity-check the right secret
        # was read — never the full value.
        print(f"Read {mode.value} from stdin: {_mask(secret)}", file=sys.stderr)

        try:
            credentials, token_type = authenticator.register_from_secret(mode, secret)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        return credentials, token_type

    # 2. Interactive OAuth (no flags) — also gated on the DECLARATION, so a scope that
    # only accepts a pasted secret says so instead of reaching the authenticator's
    # defensive UnsupportedRegistrationMode (which nothing here catches).
    _require_mode(scope, CredentialRegistrationMode.INTERACTIVE_OAUTH, "interactive OAuth")
    if not sys.stdin.isatty():
        print("Error: interactive auth requires a TTY. "
              "Use: docker compose exec -it cron ...", file=sys.stderr)
        sys.exit(1)
    try:
        auth_result = authenticate_interactive(scope, logger)
    except AuthenticationError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)
    credentials = {
        "subscription_key": auth_result.subscription_key,
        "refresh_token": auth_result.refresh_token,
        "expires_at": auth_result.expires_at,
        "subscription_type": auth_result.subscription_type,
        "id_token": auth_result.id_token,
        "raw_auth": auth_result.raw_auth,
    }
    return credentials, "oauth"


class CredentialRefreshCommand:
    @property
    def name(self) -> str:
        return "credential:refresh"

    @property
    def shortcut(self) -> str:
        return "cr:ref"

    @property
    def help(self) -> str:
        return "Re-authenticate an existing credential (interactive OAuth)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("credential_id", type=int, help="Credential ID to refresh")

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import CredentialLeasedError, register_credential
        from ..agent_manager.auth import AuthenticationError, authenticate_interactive
        from ..agent_manager.credential_store import get_credential
        from ..harness import CredentialRegistrationMode

        db_config, _, _ = _load_framework_config()
        logger = get_logger("agent-manager")

        conn = get_connection_or_exit(db_config)
        try:
            credential = get_credential(conn, args.credential_id)
            if credential is None:
                print(f"Error: credential not found: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
            if not credential.enabled:
                print(f"Error: credential is disabled: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
        finally:
            conn.close()

        # refresh is interactive-OAuth only, so it needs the same declaration gate as
        # `credential:register`; a scope that only takes a pasted secret is told to
        # re-register instead of being dropped into an unsupported flow.
        _require_mode(
            credential.scope,
            CredentialRegistrationMode.INTERACTIVE_OAUTH,
            "credential:refresh (interactive OAuth)",
        )
        if not sys.stdin.isatty():
            print("Error: interactive auth requires a TTY. "
                  "Use: docker compose exec -it cron ...", file=sys.stderr)
            sys.exit(1)

        print(f"Refreshing credential [{credential.id}] {credential.scope} {credential.label}")

        try:
            auth_result = authenticate_interactive(credential.scope, logger)
        except AuthenticationError as exc:
            print(f"Authentication failed: {exc}", file=sys.stderr)
            sys.exit(1)

        credentials = {
            "subscription_key": auth_result.subscription_key,
            "refresh_token": auth_result.refresh_token,
            "expires_at": auth_result.expires_at,
            "subscription_type": auth_result.subscription_type,
            "id_token": auth_result.id_token,
            "raw_auth": auth_result.raw_auth,
        }

        conn = get_connection_or_exit(db_config)
        try:
            try:
                refreshed = register_credential(
                    conn,
                    scope=credential.scope,
                    label=credential.label,
                    credentials=credentials,
                    token_limit=credential.token_limit,
                    logger=logger,
                )
            except CredentialLeasedError as exc:
                # The lease stops RESELECTION, not credential WRITES: overwriting the blob
                # now would be undone by the leased job's own capture, which rotates from
                # the chain it materialized before this refresh.
                conn.rollback()
                print(f"Refusing to refresh a credential in use: {exc}", file=sys.stderr)
                sys.exit(1)
            conn.commit()
        finally:
            conn.close()

        from ..events import CredentialRefreshedEvent, dispatch_credential_event
        dispatch_credential_event(
            "credential_refresh_after",
            CredentialRefreshedEvent(
                scope=credential.scope,
                credential_id=refreshed.id,
                label=refreshed.label,
                credentials=credentials,
                type=refreshed.type,
            ),
        )

        print(f"Credential [{credential.id}] refreshed successfully.")


def _humanize_delta(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _format_expiry(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if when <= now:
        return f"expired ({_humanize_delta(when, now)})"
    delta = when - now
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"in {secs}s"
    if secs < 3600:
        return f"in {secs // 60}m"
    if secs < 86400:
        return f"in {secs // 3600}h"
    return f"in {secs // 86400}d"


class CredentialListCommand:
    @property
    def name(self) -> str:
        return "credential:list"

    @property
    def shortcut(self) -> str:
        return "cr:li"

    @property
    def help(self) -> str:
        return "List registered credentials"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--scope", dest="scope", default=None,
                            help="Filter by credential scope")
        parser.add_argument("--agent-type", dest="scope", default=None,
                            help=argparse.SUPPRESS)  # deprecated alias for --scope
        parser.add_argument("--all", action="store_true", help="Include disabled credentials")
        parser.add_argument("--json", action="store_true")

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import get_usage_summaries, list_credentials

        db_config, _, am_config = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        try:
            if args.scope:
                _validate_scope(args.scope)
            credentials_rows = list_credentials(
                conn, scope=args.scope, enabled_only=not args.all
            )

            usage_map: dict[int, object] = {}
            scopes_seen = {t.scope for t in credentials_rows}
            for scope in scopes_seen:
                summaries = get_usage_summaries(conn, scope, am_config.usage_window_hours)
                for s in summaries:
                    usage_map[s.credential_id] = s
        finally:
            conn.close()

        now = datetime.now(UTC)

        if args.json:
            data = []
            for t in credentials_rows:
                s = usage_map.get(t.id)
                # MySQL SUM() yields Decimal; coerce so json.dumps can serialize.
                used = int(s.total_tokens or 0) if s else 0
                calls = int(s.call_count or 0) if s else 0
                pct_free = round((t.token_limit - used) / t.token_limit * 100, 1) if t.token_limit > 0 else None
                data.append({
                    "id": t.id,
                    # `scope` is the field going forward; `agent_type` is emitted for one
                    # cycle so existing --json consumers keep working (ROADMAP.md).
                    "scope": t.scope,
                    "agent_type": t.scope,
                    "type": t.type,
                    "priority": t.priority,
                    "label": t.label,
                    "status": t.status.value,
                    "error_msg": t.error_msg,
                    "error_source": t.error_source,
                    "used_at": t.used_at.isoformat() if t.used_at else None,
                    "expires_at": t.expires_at.isoformat() if t.expires_at else None,
                    "throttled_until": t.throttled_until.isoformat() if t.throttled_until else None,
                    "lease_owner": t.lease_owner,
                    "leased_until": t.leased_until.isoformat() if t.leased_until else None,
                    "token_limit": t.token_limit,
                    "tokens_used": used,
                    "call_count": calls,
                    "pct_free": pct_free,
                    "enabled": t.enabled,
                })
            print(json.dumps(data, indent=2))
            return

        if not credentials_rows:
            print("No credentials found.")
            return

        for t in credentials_rows:
            s = usage_map.get(t.id)
            used = s.total_tokens if s else 0
            if t.token_limit > 0:
                pct_free = round((t.token_limit - used) / t.token_limit * 100, 1)
                usage_str = f"used={used}/{t.token_limit} ({pct_free}% free)"
            else:
                usage_str = f"used={used}/unlimited"
            # A credential can be status='ok' yet temporarily skipped by the pool because it
            # hit a usage/session limit — surface that so operators aren't confused.
            throttled = t.throttled_until is not None and t.throttled_until > now.replace(tzinfo=None)
            status_str = f"status={t.status.value}" + (" (throttled)" if throttled else "")
            if t.status.value == "error":
                # Provenance decides whether it clears itself: 'auto' is lifted by the next
                # successful run, 'operator' (and pre-034 rows, provenance unknown) never is.
                status_str += f" ({t.error_source or 'operator?'})"
            leased = t.leased_until is not None and t.leased_until > now.replace(tzinfo=None)
            used_at_str = f"last_used={_humanize_delta(t.used_at, now)}"
            expires_str = f"expires={_format_expiry(t.expires_at, now)}"
            type_str = f"type={t.type}"
            prio_str = f"priority={t.priority}"
            line = (
                f"  [{t.id}] {t.scope:8} {t.label:20} "
                f"{type_str:28} {prio_str:12} "
                f"{usage_str}  {status_str}  {used_at_str}  {expires_str}"
            )
            print(line)
            if throttled:
                print(f"      ⏳ throttled until {t.throttled_until} (usage/session limit)")
            if leased:
                # A live run is rotating this credential. Do NOT token:reset/refresh it —
                # freeing the lease hands the row to a second worker mid-refresh.
                print(
                    f"      🔒 refresh lease held by {t.lease_owner} until {t.leased_until} "
                    "(a run is rotating this credential — leave it alone)"
                )
            if t.status.value == "error" and t.error_msg:
                snippet = t.error_msg[:180]
                if len(t.error_msg) > 180:
                    snippet += "…"
                print(f"      ! {snippet}")


class CredentialDeregisterCommand:
    @property
    def name(self) -> str:
        return "credential:deregister"

    @property
    def shortcut(self) -> str:
        return "cr:de"

    @property
    def help(self) -> str:
        return "Disable a credential"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("credential_id", type=int)

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import deregister_credential

        db_config, _, _ = _load_framework_config()
        logger = get_logger("agent-manager")
        conn = get_connection_or_exit(db_config)
        try:
            found = deregister_credential(conn, args.credential_id, logger=logger)
            conn.commit()
            if found:
                print(f"Deregistered credential: id={args.credential_id}")
            else:
                print(f"Credential not found: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
        finally:
            conn.close()


class CredentialMarkErrorCommand:
    @property
    def name(self) -> str:
        return "credential:mark-error"

    @property
    def shortcut(self) -> str:
        return "cr:me"

    @property
    def help(self) -> str:
        return "Manually flag a credential as unhealthy (status=error) so the pool stops using it"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("credential_id", type=int)
        parser.add_argument("message", help="Human-readable reason, stored in error_msg")

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import mark_credential_error
        from ..agent_manager.credential_store import get_credential
        from ..events import CredentialAuthFailedEvent, dispatch_credential_event

        db_config, _, _ = _load_framework_config()
        logger = get_logger("agent-manager")
        conn = get_connection_or_exit(db_config)
        try:
            credential = get_credential(conn, args.credential_id)
            found = mark_credential_error(conn, args.credential_id, args.message, logger=logger)
            conn.commit()
            if found:
                print(f"Credential [{args.credential_id}] marked as error: {args.message}")
            else:
                print(f"Credential not found: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
        finally:
            conn.close()

        if credential is not None:
            dispatch_credential_event(
                "credential_auth_failed_after",
                CredentialAuthFailedEvent(
                    scope=credential.scope,
                    credential_id=args.credential_id,
                    error_msg=args.message,
                    job_id=None,
                ),
            )


class CredentialResetCommand:
    @property
    def name(self) -> str:
        return "credential:reset"

    @property
    def shortcut(self) -> str:
        return "cr:res"

    @property
    def help(self) -> str:
        return "Clear error status on a credential so the pool starts using it again"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("credential_id", type=int)

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import clear_credential_error

        db_config, _, _ = _load_framework_config()
        logger = get_logger("agent-manager")
        conn = get_connection_or_exit(db_config)
        try:
            found = clear_credential_error(conn, args.credential_id, logger=logger)
            conn.commit()
            if found:
                print(f"Credential [{args.credential_id}] status cleared (ok)")
            else:
                print(f"Credential not found: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
        finally:
            conn.close()


class CredentialSetPriorityCommand:
    @property
    def name(self) -> str:
        return "credential:set-priority"

    @property
    def shortcut(self) -> str:
        return "cr:sp"

    @property
    def help(self) -> str:
        return "Set credential selection priority (lower wins; ties broken by LRU)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("credential_id", type=int)
        parser.add_argument("priority", type=int, help="Integer; lower = used first; default 0")

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager.credential_store import set_credential_priority

        db_config, _, _ = _load_framework_config()
        logger = get_logger("agent-manager")
        conn = get_connection_or_exit(db_config)
        try:
            found = set_credential_priority(conn, args.credential_id, args.priority, logger=logger)
            conn.commit()
            if found:
                print(f"Credential [{args.credential_id}] priority set to {args.priority}")
            else:
                print(f"Credential not found: id={args.credential_id}", file=sys.stderr)
                sys.exit(1)
        finally:
            conn.close()


class CredentialUsageCommand:
    @property
    def name(self) -> str:
        return "credential:usage"

    @property
    def shortcut(self) -> str:
        return "cr:us"

    @property
    def help(self) -> str:
        return "Show credential usage"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--scope", dest="scope", default=None,
                            help="Filter by credential scope")
        parser.add_argument("--agent-type", dest="scope", default=None,
                            help=argparse.SUPPRESS)  # deprecated alias for --scope
        parser.add_argument("--window", type=int, default=24, help="Window in hours (default: 24)")

    def execute(self, args: argparse.Namespace) -> None:
        from ..agent_manager import (
            get_credentialless_usage,
            get_usage_summaries,
            list_credentials,
        )

        db_config, _, _ = _load_framework_config()
        conn = get_connection_or_exit(db_config)
        window = args.window
        try:
            from ..harness import list_credential_scopes

            if args.scope:
                _validate_scope(args.scope)
            scopes = [args.scope] if args.scope else list_credential_scopes()
            for scope in scopes:
                credentials_rows = list_credentials(conn, scope=scope)
                summaries = get_usage_summaries(conn, scope, window)
                usage_map = {s.credential_id: s for s in summaries}
                for t in credentials_rows:
                    s = usage_map.get(t.id)
                    used = s.total_tokens if s else 0
                    calls = s.call_count if s else 0
                    limit = t.token_limit if t.token_limit else "unlimited"
                    print(f"  [{t.id}] {scope:8} {t.label:20} used={used:>10} calls={calls:>5} limit={limit}")

            # Runs by a provider that needs no credential have no row above to hang off,
            # so report them separately rather than letting them vanish from the only
            # usage surface. Suppressed when --scope narrows to a credential pool.
            if not args.scope:
                for entry in get_credentialless_usage(conn, window_hours=window):
                    label = f"{entry.harness or '?'}/{entry.provider or '?'}"
                    print(
                        f"  [--] {'(no cred)':8} {label:20} "
                        f"used={entry.total_tokens:>10} calls={entry.call_count:>5} "
                        f"limit=n/a"
                    )
        finally:
            conn.close()
