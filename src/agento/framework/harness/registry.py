"""Harness registry — the single lookup the framework uses instead of five enum-keyed maps.

One ``credential_scope`` has exactly one owning declaration: ``di.json`` carries only
the adapter's class path, so authenticator identity cannot be checked statically, and
``module:validate`` must work without importing Python. Sharing a credential pool
between harnesses would need an explicit manifest field.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .descriptor import (
    CredentialScope,
    HarnessDescriptor,
    HarnessId,
    ModelProviderDescriptor,
)
from .protocols import AgentHarnessAdapter, CredentialAuthenticator, Runner

logger = logging.getLogger(__name__)


class DuplicateHarnessError(Exception):
    """Two declarations claim the same harness id."""


class DuplicateCredentialScopeError(Exception):
    """Two declarations claim the same credential scope."""


class UnknownHarnessError(KeyError):
    """No harness registered under this id."""


class ObscureRuntimeConfigError(Exception):
    """A harness allow-listed a config field that is a secret, or does not exist."""


@dataclass(frozen=True)
class RegisteredHarness:
    descriptor: HarnessDescriptor
    adapter: AgentHarnessAdapter
    # The declaring MODULE's name — config paths are `{module}/{field}`, and a
    # module name is NOT interchangeable with the harness id (fixture
    # `fake_harness` declares harness id `fake`). Deriving the namespace from
    # `descriptor.id` silently resolves nothing.
    module: str = ""
    # Allow-list of that module's fields readable when building a command.
    runtime_config_fields: tuple[str, ...] = ()


_HARNESSES: dict[HarnessId, RegisteredHarness] = {}
_SCOPE_OWNERS: dict[CredentialScope, HarnessId] = {}


def register_harness(
    descriptor: HarnessDescriptor,
    adapter: AgentHarnessAdapter,
    module: str,
    runtime_config_fields: tuple[str, ...],
    config_schema: dict,
) -> None:
    """Register a harness. Raises on a duplicate id or a duplicate credential scope.

    ``module``/``runtime_config_fields``/``config_schema`` are positional and
    required on purpose. A default would let a missed call site register a
    silently broken channel: an empty namespace resolves nothing and an empty
    allow-list disables the channel outright — neither exposes a value, but both
    bypass declaration validation entirely, so the misconfiguration surfaces much
    later as an unexplained missing setting. A missed call site is a TypeError
    instead.
    """
    if descriptor.id in _HARNESSES:
        raise DuplicateHarnessError(
            f"Harness {descriptor.id!r} is already registered by another declaration"
        )

    scopes = descriptor.credential_scopes()
    # Duplicates WITHIN one harness: the owner check below only compares against OTHER
    # harnesses, and `set(scopes)` in the authenticator check collapses them — so two
    # providers of the same harness claiming one scope used to register cleanly. That breaks
    # the one-scope-one-pool invariant just as thoroughly as a cross-harness collision:
    # `resolve_credential_scope` would hand two different providers the same pool.
    if len(scopes) != len(set(scopes)):
        duplicated = sorted({s for s in scopes if list(scopes).count(s) > 1})
        raise DuplicateCredentialScopeError(
            f"Harness {descriptor.id!r} declares credential_scope {duplicated} on more than "
            f"one provider; a scope has exactly one owner"
        )

    for scope in scopes:
        owner = _SCOPE_OWNERS.get(scope)
        if owner is not None and owner != descriptor.id:
            raise DuplicateCredentialScopeError(
                f"credential_scope {scope!r} is already owned by harness {owner!r}; "
                f"harness {descriptor.id!r} cannot claim it too"
            )

    # The adapter must supply exactly one authenticator per credential-requiring
    # scope — a missing key would fail at registration time instead of at
    # `credential:register`, an extra key means the declaration and code disagree.
    declared = set(scopes)
    provided = set(adapter.authenticators or {})
    if declared != provided:
        raise ValueError(
            f"Harness {descriptor.id!r}: authenticators keys {sorted(provided)} do not match "
            f"its credential-requiring scopes {sorted(declared)}"
        )

    # Shape first: the manifest parser and module:validate both enforce this, but a
    # direct caller bypasses them, and the plan requires validation at BOTH layers —
    # duplicates would otherwise register verbatim, e.g. ('verbose', 'verbose').
    if not all(isinstance(f, str) and f for f in runtime_config_fields):
        raise ObscureRuntimeConfigError(
            f"Harness {descriptor.id!r}: runtime_config_fields must be non-empty strings"
        )
    if len(runtime_config_fields) != len(set(runtime_config_fields)):
        dupes = sorted(
            {f for f in runtime_config_fields if list(runtime_config_fields).count(f) > 1}
        )
        raise ObscureRuntimeConfigError(
            f"Harness {descriptor.id!r}: runtime_config_fields has duplicates {dupes}"
        )

    # Every allow-listed field must exist in the declaring module's schema and must
    # not be a secret. Secrets are declared by TYPE (`{"type": "obscure"}`) — there
    # is no `obscure: true` anywhere in the codebase, so checking for that flag
    # would never match and would admit the very field it is meant to block.
    for fname in runtime_config_fields:
        field_schema = config_schema.get(fname)
        if field_schema is None:
            raise ObscureRuntimeConfigError(
                f"Harness {descriptor.id!r}: runtime_config_fields names {fname!r}, which "
                f"module {module!r} does not declare in system.json"
            )
        if not isinstance(field_schema, dict):
            # `ModuleManifest.config` is system.json when present, but falls back to
            # module.json's `config` key, which may be {field: "default"} strings. A
            # non-dict entry carries no `type`, so we cannot prove the field is NOT a
            # secret — refuse rather than skip the check and admit it (fail closed).
            raise ObscureRuntimeConfigError(
                f"Harness {descriptor.id!r}: runtime_config_fields names {fname!r}, but "
                f"module {module!r} declares it as {type(field_schema).__name__}, not a "
                f"schema object; cannot prove it is not a secret"
            )
        if field_schema.get("type") == "obscure":
            raise ObscureRuntimeConfigError(
                f"Harness {descriptor.id!r}: runtime_config_fields names {fname!r}, which is "
                f"an 'obscure' (secret) field; secrets must never reach command construction"
            )

    _HARNESSES[HarnessId(descriptor.id)] = RegisteredHarness(
        descriptor, adapter, module, tuple(runtime_config_fields)
    )
    for scope in scopes:
        _SCOPE_OWNERS[scope] = HarnessId(descriptor.id)
    logger.debug("Registered harness %r (providers=%s)", descriptor.id, list(scopes))


def get_harness(harness: str) -> RegisteredHarness:
    """Look up a registered harness. Raises :class:`UnknownHarnessError` when absent."""
    entry = _HARNESSES.get(HarnessId(harness))
    if entry is None:
        raise UnknownHarnessError(
            f"No harness registered under {harness!r}. "
            f"Registered: {sorted(_HARNESSES)}. Has bootstrap() been called?"
        )
    return entry


def find_harness(harness: str) -> RegisteredHarness | None:
    """Look up a registered harness, or ``None`` — for callers that decide policy."""
    return _HARNESSES.get(HarnessId(harness))


def list_harnesses() -> list[RegisteredHarness]:
    """Every registered harness, ordered by id."""
    return [_HARNESSES[k] for k in sorted(_HARNESSES)]


def list_descriptors() -> list[HarnessDescriptor]:
    return [h.descriptor for h in list_harnesses()]


def resolve_provider(harness: str, provider: str) -> ModelProviderDescriptor:
    """Return the provider descriptor, raising when it is not offered by this harness."""
    descriptor = get_harness(harness).descriptor
    found = descriptor.provider(provider)
    if found is None:
        raise ValueError(
            f"Harness {harness!r} does not offer provider {provider!r}. "
            f"Available: {[p.id for p in descriptor.providers]}"
        )
    return found


def resolve_credential_scope(harness: str, provider: str) -> CredentialScope | None:
    """Credential scope for a ``(harness, provider)`` pair; ``None`` when none is required."""
    return resolve_provider(harness, provider).credential_scope


def list_credential_scopes() -> list[str]:
    """Every registered credential scope, ordered — the valid values for ``credential:*``."""
    return sorted(_SCOPE_OWNERS)


def get_harness_for_scope(scope: str) -> RegisteredHarness | None:
    """The harness owning this credential scope, or ``None`` when unknown."""
    owner = _SCOPE_OWNERS.get(CredentialScope(scope))
    return _HARNESSES[owner] if owner is not None else None


def get_authenticator(scope: str) -> CredentialAuthenticator | None:
    """Authenticator owning this credential scope, or ``None`` when the scope is unknown."""
    owner = _SCOPE_OWNERS.get(CredentialScope(scope))
    if owner is None:
        return None
    return _HARNESSES[owner].adapter.authenticators.get(CredentialScope(scope))


def account_label_for_scope(scope: str, credentials: dict | None) -> str | None:
    """The human-facing account (e.g. the OAuth e-mail) behind a decrypted credential.

    Agent-agnostic and best-effort: it dispatches to the scope's own authenticator, which
    is the only component that knows where its CLI records the authenticated account
    (Claude keeps it in ``.claude.json``'s ``oauthAccount.emailAddress``; Codex in the
    ``id_token`` JWT). The framework itself never reaches into a payload shape.

    Returns ``None`` when the scope is unknown, the authenticator does not implement
    extraction, the payload carries no account (API-key credentials), or extraction
    raises. Read via ``getattr`` so a third-party authenticator predating ``account_label``
    degrades to "unknown" instead of breaking ``credential:list``.
    """
    if not credentials:
        return None
    authenticator = get_authenticator(scope)
    if authenticator is None:
        return None
    extractor = getattr(authenticator, "account_label", None)
    if extractor is None:
        return None
    try:
        label = extractor(credentials)
    except Exception:  # never let a malformed payload break the listing/registration path
        return None
    return label if isinstance(label, str) and label.strip() else None


def clear() -> None:
    """Reset registry (for testing and consumer hot-reload)."""
    _HARNESSES.clear()
    _SCOPE_OWNERS.clear()


def workspace_adapter_for(harness: str):
    """The harness's WorkspaceAdapter. Raises when the harness is unknown.

    Strict on purpose: the old ``ConfigWriter._writers_for`` fell back to *all*
    registered writers for an unknown provider, which after the harness/provider
    split would silently pick the wrong one (``provider`` now means the model vendor,
    so every lookup by provider would miss).
    """
    return get_harness(harness).adapter.workspace_adapter


def create_runner(harness: str, ctx, *, logger=None, dry_run: bool = False) -> Runner:
    """Build a runner for ``harness`` from an already-populated run context.

    The one lookup+construct step every caller needs; ``ctx`` already carries the
    provider, model and the claimed credential, so nothing here selects anything.
    """
    return get_harness(harness).adapter.create_runner(ctx, logger=logger, dry_run=dry_run)


def owned_paths_for(harness: str) -> tuple[set[str], set[str]]:
    """``(files, dirs)`` the harness owns inside a build/run dir."""
    return workspace_adapter_for(harness).owned_paths()


def persistent_home_paths_for(harness: str) -> list[str]:
    """HOME-relative paths of this harness that must survive workspace rebuilds."""
    paths = workspace_adapter_for(harness).persistent_home_paths() or []
    return sorted({p for p in paths if p})
