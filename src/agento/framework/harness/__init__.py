"""Harness contract — the plugin boundary for agent-driving programs.

Three independent axes: **harness** (the program driving the agent), **provider**
(the model/API vendor) and **model**. A new harness registers itself through
``agent_harnesses`` in its own ``di.json``; nothing in ``src/agento/framework/``
needs to change.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from .descriptor import (
    CredentialRegistrationMode,
    CredentialScope,
    HarnessCapabilities,
    HarnessDescriptor,
    HarnessId,
    ModelProviderDescriptor,
    ProviderId,
    SandboxPackage,
)
from .manifest import (
    HarnessDeclaration,
    enumerate_harness_declarations,
    enumerate_sandbox_packages,
    parse_harness_declarations,
)
from .options_source import SUPPORTED_SOURCES, resolve_options
from .protocols import (
    AGENT_CONFIG_PREFIX,
    AgentHarnessAdapter,
    AuthResult,
    CommandBuilder,
    CredentialAuthenticator,
    ParseSummary,
    Runner,
    StreamRenderer,
    ToolUse,
    TranscriptReader,
    UnsupportedRegistrationMode,
    WorkspaceAdapter,
    get_agent_config,
    get_harness_config,
    supply_harness_config,
)
from .provider_options import (
    PROVIDER_OPTION_KEY,
    is_provider_option_hidden,
    provider_option_names,
)
from .registry import (
    DuplicateCredentialScopeError,
    DuplicateHarnessError,
    ObscureRuntimeConfigError,
    RegisteredHarness,
    UnknownHarnessError,
    account_label_for_scope,
    clear,
    create_runner,
    find_harness,
    get_authenticator,
    get_harness,
    get_harness_for_scope,
    list_credential_scopes,
    list_descriptors,
    list_harnesses,
    owned_paths_for,
    persistent_home_paths_for,
    register_harness,
    resolve_credential_scope,
    resolve_provider,
    workspace_adapter_for,
)
from .runtime import (
    HarnessRunContext,
    McpInitReport,
    McpServerStatus,
    RunRequest,
    RunResult,
    ToolboxConnectionSpec,
)
from .subprocess_runner import SubprocessRunner

_MCP_PATHS = ("/mcp", "/sse")


def toolbox_origin(url: str) -> tuple[str, str, int]:
    """Parse a trusted toolbox URL into a comparable origin, or raise.

    Raising (rather than returning None) is the whole point: a sentinel return value
    compares equal to another sentinel, so a malformed toolbox_url would match every
    malformed server entry and inject the capability into all of them.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"toolbox_url is not a usable http(s) origin: {url!r}")
    return (
        parts.scheme,
        parts.hostname.lower(),
        parts.port or (443 if parts.scheme == "https" else 80),
    )


def is_toolbox_endpoint(url: str, target: tuple[str, str, int]) -> bool:
    """True only for OUR toolbox's MCP endpoint. Any parse failure is False."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        origin = (
            parts.scheme,
            parts.hostname.lower(),
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except ValueError:
        return False
    return origin == target and parts.path.rstrip("/") in _MCP_PATHS

__all__ = [
    "AGENT_CONFIG_PREFIX",
    "PROVIDER_OPTION_KEY",
    "SUPPORTED_SOURCES",
    "AgentHarnessAdapter",
    "AuthResult",
    "CommandBuilder",
    "CredentialAuthenticator",
    "CredentialRegistrationMode",
    "CredentialScope",
    "DuplicateCredentialScopeError",
    "DuplicateHarnessError",
    "HarnessCapabilities",
    "HarnessDeclaration",
    "HarnessDescriptor",
    "HarnessId",
    "HarnessRunContext",
    "McpInitReport",
    "McpServerStatus",
    "ModelProviderDescriptor",
    "ObscureRuntimeConfigError",
    "ParseSummary",
    "ProviderId",
    "RegisteredHarness",
    "RunRequest",
    "RunResult",
    "Runner",
    "SandboxPackage",
    "StreamRenderer",
    "SubprocessRunner",
    "ToolUse",
    "ToolboxConnectionSpec",
    "TranscriptReader",
    "UnknownHarnessError",
    "UnsupportedRegistrationMode",
    "WorkspaceAdapter",
    "account_label_for_scope",
    "clear",
    "create_runner",
    "enumerate_harness_declarations",
    "enumerate_sandbox_packages",
    "find_harness",
    "get_agent_config",
    "get_authenticator",
    "get_harness",
    "get_harness_config",
    "get_harness_for_scope",
    "is_provider_option_hidden",
    "is_toolbox_endpoint",
    "list_credential_scopes",
    "list_descriptors",
    "list_harnesses",
    "owned_paths_for",
    "parse_harness_declarations",
    "persistent_home_paths_for",
    "provider_option_names",
    "register_harness",
    "resolve_credential_scope",
    "resolve_options",
    "resolve_provider",
    "supply_harness_config",
    "toolbox_origin",
    "workspace_adapter_for",
]
