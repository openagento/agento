"""Dynamic ``options_source`` resolution for ``system.json`` select fields.

Reads harness declarations OFF DISK (``enumerate_harness_declarations``), never from the
runtime registry: ``config:set`` validates a select value by loading ``system.json``
directly, with no ``bootstrap()`` and no Python imports available.
"""
from __future__ import annotations

from pathlib import Path

HARNESS_REGISTRY = "agent_harness_registry"
HARNESS_PROVIDERS = "agent_harness_providers"

SUPPORTED_SOURCES = (HARNESS_REGISTRY, HARNESS_PROVIDERS)


def resolve_options(
    source: str,
    *,
    project_root: Path | None = None,
    depends_on_value: str | None = None,
) -> list[dict[str, str]]:
    """Return ``[{"value":..., "label":...}]`` for a supported ``options_source``.

    ``depends_on_value`` is the current value of the field named by ``depends_on`` —
    for provider options that is the selected harness. When it is unknown, every
    harness's providers are offered (prefixed) rather than nothing, so the field is
    still usable before a harness has been picked.
    """
    from ..module_discovery import resolve_module_root
    from .manifest import enumerate_harness_declarations

    if source not in SUPPORTED_SOURCES:
        raise ValueError(
            f"Unknown options_source {source!r}. Supported: {list(SUPPORTED_SOURCES)}"
        )

    # Default to the ambient root rather than "core modules only" — an explicit
    # project_root (tests, install-time) still wins.
    if project_root is None:
        project_root = resolve_module_root()
    declarations = enumerate_harness_declarations(project_root)

    if source == HARNESS_REGISTRY:
        return [
            {"value": str(d.descriptor.id), "label": d.descriptor.label}
            for d in declarations
        ]

    options: list[dict[str, str]] = []
    for d in declarations:
        if depends_on_value and str(d.descriptor.id) != depends_on_value:
            continue
        for provider in d.descriptor.providers:
            label = provider.label
            if not depends_on_value:
                label = f"{d.descriptor.label}: {provider.label}"
            options.append({"value": str(provider.id), "label": label})
    return options
