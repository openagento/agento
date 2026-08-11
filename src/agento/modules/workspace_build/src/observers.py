"""Observers for the workspace_build module."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from agento.framework.agent_manager.models import CredentialRecord, CredentialStatus
from agento.framework.database_config import DatabaseConfig
from agento.framework.db import get_connection
from agento.framework.harness import get_harness_for_scope
from agento.framework.workspace_paths import BUILD_DIR

logger = logging.getLogger(__name__)

_EPOCH = datetime(2000, 1, 1)


def _credential_from_event(event) -> CredentialRecord:
    """Construct a minimal CredentialRecord from a credential register/refresh event."""
    return CredentialRecord(
        id=getattr(event, "credential_id", 0),
        scope=event.scope,
        type=getattr(event, "type", None) or "oauth",
        label=getattr(event, "label", ""),
        credentials=getattr(event, "credentials", {}),
        token_limit=0,
        enabled=True,
        status=CredentialStatus.OK,
        priority=0,
        error_msg=None,
        expires_at=None,
        used_at=None,
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )


def _builds_for_scope(scope: str) -> list[Path]:
    """Resolved ``current`` build dirs whose agent_view actually uses ``scope``.

    Least privilege: a ``codex`` credential refresh must not rewrite the build of a
    view running on Claude. Build layout is
    ``<BUILD_DIR>/<workspace>/<agent_view>/current``, so each candidate's effective
    ``(harness, provider)`` is resolved from its agent_view before deciding.
    """
    from agento.framework.agent_view_runtime import resolve_agent_view_runtime
    from agento.framework.harness import resolve_credential_scope
    from agento.framework.workspace import get_agent_view_by_code

    build_root = Path(BUILD_DIR)
    if not build_root.is_dir():
        return []

    candidates: list[tuple[str, Path]] = []
    for current in build_root.glob("*/*/current"):
        try:
            target = current.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if target.is_dir():
            candidates.append((current.parent.name, target))
    if not candidates:
        return []

    try:
        conn = get_connection(DatabaseConfig.from_env())
    except Exception:
        logger.warning(
            "Could not open DB connection to match build dirs to scope %r; skipping "
            "to avoid writing a credential into an unrelated view's build.",
            scope, exc_info=True,
        )
        return []

    matched: list[Path] = []
    try:
        for agent_view_code, target in candidates:
            try:
                agent_view = get_agent_view_by_code(conn, agent_view_code)
                if agent_view is None:
                    continue
                runtime = resolve_agent_view_runtime(conn, agent_view.id)
                if not runtime.harness or not runtime.provider:
                    continue
                if resolve_credential_scope(runtime.harness, runtime.provider) == scope:
                    matched.append(target)
            except Exception:
                logger.debug(
                    "Could not resolve scope for build dir %s; skipping",
                    target, exc_info=True,
                )
    finally:
        conn.close()
    return matched


class BuildFreshnessCheckObserver:
    """Rebuild the workspace if the resolved scoped config drifted from the
    on-disk build. Fires on ``workspace_build_check_before`` dispatched by
    the consumer at job-claim time.

    Idempotent — ``execute_build`` skips when the checksum matches and the
    build_dir is intact. Rebuilds otherwise (harness switch, model change,
    skill changes, instructions, mcp/servers, persistent-path contract drift
    after an agento-core upgrade).

    Captures exceptions on the event so the consumer can re-raise them
    (EventManager.dispatch swallows raised exceptions, but a silent rebuild
    failure would let the job run with a stale build — the exact bug this
    observer was introduced to prevent)."""

    def execute(self, event) -> None:
        agent_view_id = getattr(event, "agent_view_id", None)
        if agent_view_id is None:
            return

        from .builder import execute_build

        try:
            conn = get_connection(DatabaseConfig.from_env())
        except Exception as exc:
            event.error = exc
            logger.exception(
                "BuildFreshnessCheckObserver: could not open DB connection",
            )
            return

        try:
            execute_build(conn, agent_view_id)
        except Exception as exc:
            event.error = exc
            logger.exception(
                "BuildFreshnessCheckObserver: execute_build failed for "
                "agent_view_id=%s", agent_view_id,
            )
        finally:
            conn.close()


class RefreshBuildCredentialsObserver:
    """Re-materialize a refreshed credential into the builds that actually use it.

    Fires on ``credential_refresh_after`` / ``credential_register_after``. Without
    this, ``workspace/build/<ws>/<av>/current/...credentials`` keeps the pre-refresh
    credential that the provider has already invalidated, so ``agento run`` falls
    back to an interactive login prompt.

    Only builds whose effective credential scope equals the event's scope are
    touched — a Claude refresh never rewrites a Codex build, and no view receives a
    credential for a harness it does not run.
    """

    def execute(self, event) -> None:
        scope = getattr(event, "scope", None)
        credentials = getattr(event, "credentials", None)
        if not scope or not credentials:
            return

        registered = get_harness_for_scope(scope)
        if registered is None:
            logger.debug(
                "No harness owns credential scope %s; nothing to re-materialize.", scope
            )
            return
        adapter = registered.adapter.workspace_adapter
        credential = _credential_from_event(event)

        updated = 0
        for target in _builds_for_scope(scope):
            try:
                adapter.write_credentials(target, credential)
                updated += 1
                logger.info("Refreshed %s credentials in build dir: %s", scope, target)
            except Exception:
                logger.warning(
                    "Failed to refresh %s credentials in build dir: %s",
                    scope, target, exc_info=True,
                )

        if updated:
            logger.info(
                "Refreshed %s credentials across %d build dir(s).", scope, updated
            )


class ReplaceErroredTokenCredentialsObserver:
    """When a credential flips to ``status='error'``, re-materialize the builds that
    use its scope with the next LRU healthy credential for the same scope.

    Fires on ``credential_auth_failed_after`` (dispatched by both the consumer's
    auth-failure path and the manual ``credential:mark-error`` CLI). Without this,
    builds keep the dead refresh token from the errored row and every subsequent
    ``agento run`` fails with the same auth error.

    Skips silently when no healthy alternative exists for that scope — the next
    ``workspace:build`` will surface the "no healthy credentials" diagnostic from
    ``CredentialResolver``.
    """

    def execute(self, event) -> None:
        scope = getattr(event, "scope", None)
        if not scope:
            return

        registered = get_harness_for_scope(scope)
        if registered is None:
            logger.debug(
                "No harness owns credential scope %s; nothing to re-materialize.", scope
            )
            return
        adapter = registered.adapter.workspace_adapter

        from agento.framework.agent_manager.credential_resolver import CredentialResolver

        try:
            conn = get_connection(DatabaseConfig.from_env())
        except Exception:
            logger.warning(
                "Could not open DB connection to resolve replacement credential for %s",
                scope, exc_info=True,
            )
            return

        try:
            try:
                replacement = CredentialResolver().resolve(conn, scope)
            except RuntimeError as exc:
                logger.warning(
                    "No healthy %s credential available to replace errored one: %s",
                    scope, exc,
                )
                return
        finally:
            conn.close()

        if replacement.credentials is None:
            logger.warning(
                "Replacement %s credential id=%d has no credentials payload; skipping.",
                scope, replacement.id,
            )
            return

        updated = 0
        for target in _builds_for_scope(scope):
            try:
                adapter.write_credentials(target, replacement)
                updated += 1
                logger.info(
                    "Replaced errored %s credentials in build dir %s with credential id=%d",
                    scope, target, replacement.id,
                )
            except Exception:
                logger.warning(
                    "Failed to replace errored %s credentials in build dir: %s",
                    scope, target, exc_info=True,
                )

        if updated:
            logger.info(
                "Replaced %s credentials with credential id=%d across %d build dir(s).",
                scope, replacement.id, updated,
            )
