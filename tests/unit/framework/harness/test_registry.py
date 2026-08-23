"""The harness registry replaced five enum-keyed maps with one open registry.

These tests assert the properties that make it open: a harness the framework has never
heard of registers from its own ``di.json``, one credential scope has exactly one owner,
and every lookup the framework performs resolves through the registry rather than a
hardcoded provider name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agento.framework.harness import (
    DuplicateCredentialScopeError,
    DuplicateHarnessError,
    UnknownHarnessError,
    clear,
    create_runner,
    find_harness,
    get_authenticator,
    get_harness,
    get_harness_for_scope,
    list_credential_scopes,
    list_harnesses,
    parse_harness_declarations,
    persistent_home_paths_for,
    register_harness,
    resolve_credential_scope,
    resolve_provider,
    workspace_adapter_for,
)
from agento.framework.module_loader import import_class
from tests.harness_fixtures import register_builtin_harnesses, register_module_harness

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "modules"


def _register_fixture(module: str) -> None:
    module_dir = FIXTURES / module
    for decl in parse_harness_declarations(module_dir / "di.json", module):
        adapter = import_class(module_dir, decl.class_path)()
        register_harness(decl.descriptor, adapter, decl.module,
                         decl.runtime_config_fields, _module_config_schema(module_dir))


@pytest.fixture(autouse=True)
def _clean_registry():
    clear()
    yield
    clear()


class TestThirdPartyHarness:
    """A harness no framework file mentions still works end to end."""

    def test_registers_from_its_own_di_json(self):
        _register_fixture("fake_harness")

        registered = get_harness("fake")
        assert registered.descriptor.label == "Fake Harness"
        assert [p.id for p in registered.descriptor.providers] == [
            "fake_local", "fake_cloud",
        ]

    def test_framework_source_never_names_it(self):
        framework = Path(__file__).resolve().parents[3].parent / "src" / "agento" / "framework"
        offenders = [
            p for p in framework.rglob("*.py")
            if "fake_cloud" in p.read_text() or '"fake"' in p.read_text()
        ]
        assert offenders == [], f"framework hardcodes the test harness: {offenders}"

    def test_two_providers_differ_in_credential_requirement(self):
        """The axis the old closed enum could not express: one harness, two providers,
        only one of which needs a credential."""
        _register_fixture("fake_harness")

        local = resolve_provider("fake", "fake_local")
        cloud = resolve_provider("fake", "fake_cloud")

        assert local.credential_required is False
        assert local.credential_scope is None
        assert resolve_credential_scope("fake", "fake_local") is None

        assert cloud.credential_required is True
        assert resolve_credential_scope("fake", "fake_cloud") == "fake_cloud"

    def test_credential_scopes_exclude_the_credentialless_provider(self):
        _register_fixture("fake_harness")
        assert list_credential_scopes() == ["fake_cloud"]

    def test_authenticator_resolves_by_scope(self):
        _register_fixture("fake_harness")

        assert get_authenticator("fake_cloud") is not None
        # A provider that needs no credential has no authenticator to find.
        assert get_authenticator("fake_local") is None
        assert get_harness_for_scope("fake_cloud").descriptor.id == "fake"

    def test_harness_without_transcript_reader_is_allowed(self):
        _register_fixture("fake_harness")
        assert get_harness("fake").adapter.transcript_reader is None

    def test_create_runner_goes_through_the_adapter(self):
        from agento.framework.harness import HarnessRunContext

        _register_fixture("fake_harness")
        ctx = HarnessRunContext(harness="fake", provider="fake_local", credential_required=False)

        runner = create_runner("fake", ctx, logger=None, dry_run=True)

        assert runner.context is ctx
        assert runner.command_builder is get_harness("fake").adapter.command_builder

    def test_no_persistent_home_paths_is_empty_not_an_error(self):
        _register_fixture("fake_harness")
        assert persistent_home_paths_for("fake") == []


class TestScopeOwnership:
    def test_second_claimant_of_a_scope_is_rejected(self):
        """One credential pool, one owner. ``di.json`` carries only a class path, so
        authenticator identity can't be checked statically — sharing a pool would need
        an explicit manifest field."""
        register_module_harness("claude")

        with pytest.raises(DuplicateCredentialScopeError, match="claude"):
            _register_fixture("scope_collision")

    def test_duplicate_harness_id_is_rejected(self):
        register_module_harness("claude")
        with pytest.raises(DuplicateHarnessError, match="claude"):
            register_module_harness("claude")

    def test_authenticators_must_match_credential_requiring_scopes(self):
        """A declaration promising a credential-bearing provider whose adapter has no
        authenticator fails at REGISTRATION, not at ``credential:register``."""
        module_dir = FIXTURES / "fake_harness"
        (decl,) = parse_harness_declarations(module_dir / "di.json", "fake_harness")
        adapter = import_class(module_dir, decl.class_path)()
        adapter._authenticators = {}  # simulate a code/declaration mismatch

        with pytest.raises(ValueError, match="authenticators keys"):
            register_harness(decl.descriptor, adapter, decl.module,
                         decl.runtime_config_fields, _module_config_schema(module_dir))


class TestLookupFailures:
    def test_unknown_harness_raises_with_a_registered_list(self):
        register_builtin_harnesses()
        with pytest.raises(UnknownHarnessError, match="claude"):
            get_harness("nope")

    def test_find_harness_returns_none_for_callers_that_decide_policy(self):
        register_builtin_harnesses()
        assert find_harness("nope") is None
        assert find_harness("claude") is not None

    def test_workspace_adapter_for_is_strict(self):
        """The old ``ConfigWriter._writers_for`` fell back to ALL registered writers for
        an unknown provider; post-split that would silently pick the wrong harness."""
        register_builtin_harnesses()
        with pytest.raises(UnknownHarnessError):
            workspace_adapter_for("nope")

    def test_provider_not_offered_by_this_harness_raises(self):
        register_builtin_harnesses()
        with pytest.raises(ValueError, match="does not offer provider"):
            resolve_provider("claude", "openai")

    def test_clear_empties_the_registry_and_its_scope_index(self):
        register_builtin_harnesses()
        assert list_harnesses()
        clear()
        assert list_harnesses() == []
        assert list_credential_scopes() == []


def _module_config_schema(module_dir):
    """Read the fixture module's system.json (empty when it has none)."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(module_dir) / "system.json"
    if not p.is_file():
        return {}
    try:
        data = _json.loads(p.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
