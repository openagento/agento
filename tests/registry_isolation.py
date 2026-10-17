"""Keep the integration ``bootstrap()`` out of unit tests.

``bootstrap()`` fills process-global registries (module configs, observers, harnesses,
workflows, ...). Under pytest-xdist one worker runs unit and integration tests in any
order, so a unit test can run right after an integration test in the same process and
see a fully bootstrapped framework (e.g. observers that open a real DB connection).
"""
from __future__ import annotations

from agento.framework import bootstrap as _bootstrap
from agento.framework.channels import registry as channel_registry
from agento.framework.commands import clear as clear_commands
from agento.framework.event_manager import clear as clear_event_manager
from agento.framework.harness import clear as clear_harnesses
from agento.framework.ingress_identity import clear_regex_identity_types
from agento.framework.onboarding import clear as clear_onboardings
from agento.framework.router_registry import clear as clear_routers
from agento.framework.workflows import clear as clear_workflows

_bootstrapped = False


def is_bootstrapped() -> bool:
    return _bootstrapped


def mark_bootstrapped() -> None:
    global _bootstrapped
    _bootstrapped = True


def clear_registries() -> None:
    """Return every registry ``bootstrap()`` fills to the state of a fresh process."""
    global _bootstrapped
    channel_registry.clear()
    clear_workflows()
    clear_harnesses()
    clear_commands()
    clear_onboardings()
    clear_routers()
    clear_regex_identity_types()
    clear_event_manager()
    _bootstrap._MODULE_CONFIGS.clear()
    _bootstrap._MANIFESTS.clear()
    _bootstrapped = False
