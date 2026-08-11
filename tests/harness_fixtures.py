"""Shared helpers for registering real harnesses in unit tests.

Unit tests don't run the full ``bootstrap()`` module loader, but they do need the
harness registry populated so ``workspace_adapter_for`` / ``get_harness`` resolve.
Registration goes through the same ``di.json`` declarations the framework uses, so a
test can never register a harness shape that the real loader would reject.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from agento.framework.harness import (
    clear,
    parse_harness_declarations,
    register_harness,
)

MODULES_ROOT = Path(__file__).resolve().parents[1] / "src" / "agento" / "modules"


def register_module_harness(module: str) -> None:
    """Register the harness declared by a core module's ``di.json``."""
    from agento.framework.module_loader import import_class

    module_dir = MODULES_ROOT / module
    for decl in parse_harness_declarations(module_dir / "di.json", module):
        adapter = import_class(module_dir, decl.class_path)()
        register_harness(decl.descriptor, adapter)


def register_builtin_harnesses() -> None:
    """Register both shipped harnesses (claude + codex) from a clean registry."""
    clear()
    register_module_harness("claude")
    register_module_harness("codex")


def make_runner(
    harness: str = "claude",
    *,
    credential=None,
    model: str | None = None,
    working_dir: str = "/workspace",
    home_dir: str | None = None,
    timeout_seconds: int = 1200,
    extra_env: dict[str, str] | None = None,
    credential_required: bool | None = None,
    dry_run: bool = False,
    logger=None,
):
    """Build a real runner for ``harness`` the way the consumer does.

    The caller owns credential selection now, so tests pass the credential in via the
    context instead of a ``token_override`` kwarg on the runner.
    """
    from agento.framework.harness import HarnessRunContext, find_harness, get_harness

    if find_harness(harness) is None:
        register_module_harness(harness)
    registered = get_harness(harness)
    descriptor = registered.descriptor
    provider = descriptor.provider(descriptor.default_provider)
    ctx = HarnessRunContext(
        harness=harness,
        provider=descriptor.default_provider,
        model=model,
        working_dir=working_dir,
        home_dir=home_dir,
        timeout_seconds=timeout_seconds,
        credential_required=(
            provider.credential_required if credential_required is None else credential_required
        ),
        credential=credential,
        extra_env=extra_env or {},
    )
    return registered.adapter.create_runner(ctx, dry_run=dry_run, logger=logger)


@contextmanager
def stub_workspace_adapters(**adapters):
    """Substitute the WorkspaceAdapter used for one or more harness ids.

    Replaces the old ``patch("agento.framework.harness._CONFIG_WRITERS", {...})``: the
    adapter now hangs off the registered harness, so tests stub the lookup instead of a
    module-level dict. Keyword keys are harness ids.
    """
    from agento.framework.harness import UnknownHarnessError

    def _lookup(harness):
        try:
            return adapters[harness]
        except KeyError:
            raise UnknownHarnessError(f"No harness registered under {harness!r}") from None

    # Patch every binding, not just the package attribute: modules that did
    # ``from .harness import workspace_adapter_for`` hold their own reference, and
    # ``owned_paths_for``/``persistent_home_paths_for`` resolve the registry global.
    import sys

    mock = MagicMock(side_effect=_lookup)
    with ExitStack() as stack:
        for name, module in list(sys.modules.items()):
            if name.startswith("agento.") and hasattr(module, "workspace_adapter_for"):
                stack.enter_context(patch.object(module, "workspace_adapter_for", mock))
        yield mock
