"""Static harness metadata — descriptors built from ``di.json`` declarations.

Three independent axes replace the old closed ``AgentProvider`` enum:

- **harness** — the program driving the agent (runner, command builder, workspace
  adapter, transcript reader, sandbox package)
- **provider** — the model/API vendor (credential requirement, credential scope)
- **model** — the model identifier at that provider (the ``--model`` flag)

Descriptors are pure data. They are built by :mod:`agento.framework.harness.manifest`
straight from ``di.json`` (no Python import needed), so ``config:set``,
``enumerate_sandbox_packages`` and ``module:validate`` can enumerate harnesses
without a ``bootstrap()``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType

HarnessId = NewType("HarnessId", str)
ProviderId = NewType("ProviderId", str)
CredentialScope = NewType("CredentialScope", str)

# Identifiers land in ``credential.scope VARCHAR(64)`` and in Docker ARG names, so
# they are bounded and canonical. Validated at manifest level, not at INSERT time.
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ID_MAX_LENGTH = 64


class CredentialRegistrationMode(StrEnum):
    """HOW a credential is obtained. A provider declares every mode it supports.

    Deliberately not fused with "is a credential required at all" — Claude supports
    OAuth *and* API key, Codex additionally an access token, so the modes are not
    mutually exclusive alternatives to "none".
    """

    INTERACTIVE_OAUTH = "interactive_oauth"
    API_KEY = "api_key"
    ACCESS_TOKEN = "access_token"


@dataclass(frozen=True)
class HarnessCapabilities:
    """What a harness can do. Consumers branch on capabilities, never on harness id."""

    interactive: bool = False
    resume: bool = False
    transcripts: bool = False
    structured_events: bool = False
    toolbox: bool = False
    mcp_init_report: bool = False
    usage_reporting: bool = False
    cost_reporting: bool = False

    @classmethod
    def from_declaration(cls, decl: dict | None) -> HarnessCapabilities:
        if decl is None:
            decl = {}
        if not isinstance(decl, dict):
            raise ValueError("harness capabilities must be an object")
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(decl) - known
        if unknown:
            raise ValueError(
                f"Unknown harness capabilities: {sorted(unknown)}. Known: {sorted(known)}"
            )
        # Require real JSON booleans. `bool(v)` would read the STRING "false" as True and
        # silently advertise a capability the harness does not have — the framework then
        # branches on it and fails at runtime instead of at manifest-validation time.
        for key, value in decl.items():
            if not isinstance(value, bool):
                raise ValueError(
                    f"harness capability {key!r} must be true or false, got "
                    f"{type(value).__name__} {value!r}"
                )
        return cls(**decl)


@dataclass(frozen=True)
class ModelProviderDescriptor:
    """One model/API vendor offered by a harness."""

    id: ProviderId
    label: str
    credential_required: bool
    registration_modes: tuple[CredentialRegistrationMode, ...] = ()
    credential_scope: CredentialScope | None = None

    @classmethod
    def from_declaration(cls, decl: dict) -> ModelProviderDescriptor:
        provider_id = _require_id(decl, "id", "provider")
        required = decl.get("credential_required", False)
        if not isinstance(required, bool):
            raise ValueError(
                f"Provider {provider_id!r}: credential_required must be true or false, "
                f"got {type(required).__name__} {required!r}"
            )
        raw_modes = decl.get("registration_modes", []) or []
        if not isinstance(raw_modes, list):
            raise ValueError(
                f"Provider {provider_id!r}: registration_modes must be an array"
            )
        modes: list[CredentialRegistrationMode] = []
        for raw in raw_modes:
            try:
                mode = CredentialRegistrationMode(raw)
            except ValueError:
                raise ValueError(
                    f"Provider {provider_id!r}: unknown registration mode {raw!r}. "
                    f"Known: {[m.value for m in CredentialRegistrationMode]}"
                ) from None
            if mode in modes:
                raise ValueError(
                    f"Provider {provider_id!r}: duplicate registration mode {raw!r}"
                )
            modes.append(mode)

        scope = decl.get("credential_scope")
        if scope is not None:
            _validate_id(str(scope), f"provider {provider_id!r} credential_scope")

        # credential_required is the single source of truth; scope and modes must
        # agree with it in both directions so a half-declared provider cannot load.
        if required:
            if not scope:
                raise ValueError(
                    f"Provider {provider_id!r}: credential_required=true needs a credential_scope"
                )
            if not modes:
                raise ValueError(
                    f"Provider {provider_id!r}: credential_required=true needs "
                    f"a non-empty registration_modes"
                )
        else:
            if scope:
                raise ValueError(
                    f"Provider {provider_id!r}: credential_required=false must not "
                    f"declare a credential_scope"
                )
            if modes:
                raise ValueError(
                    f"Provider {provider_id!r}: credential_required=false must not "
                    f"declare registration_modes"
                )

        return cls(
            id=ProviderId(provider_id),
            label=str(decl.get("label") or provider_id),
            credential_required=required,
            registration_modes=tuple(modes),
            credential_scope=CredentialScope(str(scope)) if scope else None,
        )


@dataclass(frozen=True)
class SandboxPackage:
    """How a harness's CLI is installed into the sandbox image.

    Rendered into the sandbox Dockerfile, so every field is validated against a
    closed schema: a third-party ``di.json`` must not be able to inject shell.
    """

    harness: HarnessId
    manager: str
    package: str
    binary: str
    version_env_key: str
    default_range: str

    # Only managers with a dedicated install template are accepted. The rendered
    # command is chosen by this key, never built out of the value itself.
    SUPPORTED_MANAGERS = ("npm",)

    PACKAGE_PATTERN = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
    BINARY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
    ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
    VERSION_PATTERN = re.compile(r"^[~^]?\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")

    @classmethod
    def from_declaration(cls, decl: dict, harness: str) -> SandboxPackage:
        def _field(name: str) -> str:
            value = decl.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"sandbox_package for harness {harness!r}: {name!r} must be a non-empty string"
                )
            return value

        manager = decl.get("manager", "npm")
        if manager not in cls.SUPPORTED_MANAGERS:
            raise ValueError(
                f"sandbox_package for harness {harness!r}: unsupported manager {manager!r}. "
                f"Supported: {list(cls.SUPPORTED_MANAGERS)}"
            )
        package, binary = _field("package"), _field("binary")
        env_key, default_range = _field("version_env_key"), _field("default_range")
        for value, pattern, name in (
            (package, cls.PACKAGE_PATTERN, "package"),
            (binary, cls.BINARY_PATTERN, "binary"),
            (env_key, cls.ENV_KEY_PATTERN, "version_env_key"),
            (default_range, cls.VERSION_PATTERN, "default_range"),
        ):
            if not pattern.fullmatch(value):
                raise ValueError(
                    f"sandbox_package for harness {harness!r}: {name}={value!r} does not match "
                    f"{pattern.pattern} — refusing to render it into a Dockerfile"
                )
        return cls(
            harness=HarnessId(harness),
            manager=manager,
            package=package,
            binary=binary,
            version_env_key=env_key,
            default_range=default_range,
        )


@dataclass(frozen=True)
class HarnessDescriptor:
    """Everything the framework knows about a harness without importing its code."""

    id: HarnessId
    label: str
    providers: tuple[ModelProviderDescriptor, ...]
    default_provider: ProviderId
    capabilities: HarnessCapabilities = field(default_factory=HarnessCapabilities)
    sandbox_package: SandboxPackage | None = None

    @classmethod
    def from_declaration(cls, decl: dict) -> HarnessDescriptor:
        harness_id = _require_id(decl, "id", "harness")
        raw_providers = decl.get("providers") or []
        if not isinstance(raw_providers, list) or not raw_providers:
            raise ValueError(f"Harness {harness_id!r}: providers must be a non-empty array")

        providers: list[ModelProviderDescriptor] = []
        seen: set[str] = set()
        for raw in raw_providers:
            if not isinstance(raw, dict):
                raise ValueError(f"Harness {harness_id!r}: each provider must be an object")
            provider = ModelProviderDescriptor.from_declaration(raw)
            if provider.id in seen:
                raise ValueError(
                    f"Harness {harness_id!r}: duplicate provider id {provider.id!r}"
                )
            seen.add(provider.id)
            providers.append(provider)

        default_provider = decl.get("default_provider")
        if not default_provider:
            raise ValueError(f"Harness {harness_id!r}: default_provider is required")
        if default_provider not in seen:
            raise ValueError(
                f"Harness {harness_id!r}: default_provider {default_provider!r} is not among "
                f"its providers {sorted(seen)}"
            )

        sandbox = decl.get("sandbox_package")
        if sandbox is not None and not isinstance(sandbox, dict):
            # Would otherwise surface as a TypeError/AttributeError deep inside
            # from_declaration instead of a normal manifest-validation error.
            raise ValueError(
                f"Harness {harness_id!r}: sandbox_package must be an object, got "
                f"{type(sandbox).__name__}"
            )
        return cls(
            id=HarnessId(harness_id),
            label=str(decl.get("label") or harness_id),
            providers=tuple(providers),
            default_provider=ProviderId(str(default_provider)),
            capabilities=HarnessCapabilities.from_declaration(decl.get("capabilities")),
            sandbox_package=(
                SandboxPackage.from_declaration(sandbox, harness_id) if sandbox else None
            ),
        )

    def provider(self, provider_id: str) -> ModelProviderDescriptor | None:
        """Return the provider descriptor with this id, or ``None``."""
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None

    def credential_scopes(self) -> tuple[CredentialScope, ...]:
        """Scopes of every provider that requires a credential."""
        return tuple(
            p.credential_scope
            for p in self.providers
            if p.credential_required and p.credential_scope
        )


def _validate_id(value: str, what: str) -> None:
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{what}: {value!r} must match {ID_PATTERN.pattern}")
    if len(value) > ID_MAX_LENGTH:
        raise ValueError(
            f"{what}: {value!r} exceeds {ID_MAX_LENGTH} characters "
            f"(credential.scope is VARCHAR({ID_MAX_LENGTH}))"
        )


def _require_id(decl: dict, key: str, what: str) -> str:
    value = decl.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} declaration: {key!r} must be a non-empty string")
    _validate_id(value, f"{what} {key}")
    return value
