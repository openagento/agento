"""Which ``agent_view/provider_options/<name>`` fields apply to the selected provider.

A self-hosted provider needs an endpoint override; a hosted one does not. Rather than
showing every operator an empty "Provider base URL" box that matters to one provider,
a ``system.json`` field names the provider option it belongs to::

    "provider_options/base_url": {"type": "string", "provider_option": "base_url"}

and each provider declares the options it needs in its module's ``di.json``::

    {"id": "ollama", "credential_required": false, "provider_options": ["base_url"]}

The condition therefore lives with the provider, not in the core module — no part of
``agent_view`` or the framework names a provider, which is the same agent-agnosticism
rule that keeps provider ids out of framework branches.

Declarations are read OFF DISK (``enumerate_harness_declarations``), never from the
runtime registry, so this works in ``config:*`` and admin without a ``bootstrap()``.
"""
from __future__ import annotations

from pathlib import Path

PROVIDER_OPTION_KEY = "provider_option"


def provider_option_names(
    harness: str | None,
    provider: str | None,
    *,
    project_root: Path | None = None,
) -> frozenset[str]:
    """The provider-option names declared by ``provider`` under ``harness``.

    Empty when either id is unknown or unset — callers must treat "no information"
    as "do not hide", never as "hide everything" (see :func:`is_provider_option_hidden`).
    """
    if not harness or not provider:
        return frozenset()

    from ..module_discovery import resolve_module_root
    from .manifest import enumerate_harness_declarations

    if project_root is None:
        project_root = resolve_module_root()
    for declaration in enumerate_harness_declarations(project_root):
        if str(declaration.descriptor.id) != harness:
            continue
        for candidate in declaration.descriptor.providers:
            if str(candidate.id) == provider:
                return frozenset(candidate.provider_options)
    return frozenset()


def is_provider_option_hidden(
    field_schema: dict,
    *,
    harness: str | None,
    provider: str | None,
    project_root: Path | None = None,
) -> bool:
    """True when this field belongs to a provider option the selection does not have.

    Hides only on POSITIVE knowledge: a field is hidden when the effective provider is
    known AND does not declare the option. An unset or unresolvable harness/provider
    leaves the field visible, because hiding a field the operator still needs to set is
    the worse failure — and at the default scope there may legitimately be no provider
    selected yet.
    """
    option = field_schema.get(PROVIDER_OPTION_KEY)
    if not option:
        return False
    if not harness or not provider:
        return False
    declared = provider_option_names(harness, provider, project_root=project_root)
    if not declared and not _provider_exists(harness, provider, project_root=project_root):
        # An unresolvable provider (module disabled, typo'd config) is missing
        # information, not a provider that declared nothing — stay visible.
        return False
    return str(option) not in declared


def _provider_exists(
    harness: str, provider: str, *, project_root: Path | None = None
) -> bool:
    from ..module_discovery import resolve_module_root
    from .manifest import enumerate_harness_declarations

    if project_root is None:
        project_root = resolve_module_root()
    return any(
        str(candidate.id) == provider
        for declaration in enumerate_harness_declarations(project_root)
        if str(declaration.descriptor.id) == harness
        for candidate in declaration.descriptor.providers
    )
