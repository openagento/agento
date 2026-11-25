"""Dynamic ``options_source`` resolution for ``system.json`` select fields.

Reads harness declarations OFF DISK (``enumerate_harness_declarations``), never from the
runtime registry: ``config:set`` validates a select value by loading ``system.json``
directly, with no ``bootstrap()`` and no Python imports available.
"""
from __future__ import annotations

import functools
from pathlib import Path

HARNESS_REGISTRY = "agent_harness_registry"
HARNESS_PROVIDERS = "agent_harness_providers"
# Not harness data, but this is the one options_source registry (the validator reads it).
TIMEZONES = "timezones"
# An option is data and shows as sent. The one fixed option is a text: its message is
# ``config.option.<label_key>`` in the ``panel`` catalog, and ``label`` is the English source.
BROWSER_TIMEZONE = {"value": "browser", "label": "Browser time zone", "label_key": "browser_timezone"}
PANEL_LANGUAGES = "panel_languages"

SUPPORTED_SOURCES = (HARNESS_REGISTRY, HARNESS_PROVIDERS, TIMEZONES, PANEL_LANGUAGES)


@functools.cache
def _timezone_options() -> tuple[dict[str, str], ...]:
    """Cached: ``available_timezones()`` walks TZPATH, and tzdata changes only with a restart."""
    import zoneinfo

    return (
        BROWSER_TIMEZONE,
        *({"value": z, "label": z} for z in sorted(zoneinfo.available_timezones())),
    )


def resolve_options(
    source: str,
    *,
    project_root: Path | None = None,
    depends_on_value: str | None = None,
) -> list[dict[str, str]]:
    """Return ``[{"value":..., "label":...}]`` for a supported ``options_source``.

    A fixed option of a source (``BROWSER_TIMEZONE``) also has ``label_key``.

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
    if source == TIMEZONES:
        return [dict(o) for o in _timezone_options()]
    if source == PANEL_LANGUAGES:
        from .. import i18n

        return [{"value": tag, "label": name} for tag, name in i18n.languages().items()]

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
