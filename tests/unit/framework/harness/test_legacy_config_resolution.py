"""Splitting one config key into two has to stay compatible with deployments that set
only the old one.

Before 0.15, ``agent_view/provider`` held what is now the HARNESS. Because the new
``config.json`` always ships a default harness, "harness unset" never happens — so the
fallback compares the two values' ORIGINS (ENV > agent_view > workspace > default >
config.json) instead of mere presence. A legacy value is recognised structurally (a
provider naming a REGISTERED HARNESS), never by a "claude"/"codex" literal, which would
put back in the framework the exact branch the harness contract removes (AGENTS.md #6).
"""
from __future__ import annotations

import pytest

from agento.framework.agent_view_runtime import _resolve_harness_and_provider
from agento.framework.scoped_config import Scope
from tests.harness_fixtures import register_builtin_harnesses

pytestmark = pytest.mark.usefixtures("builtin_harnesses")


@pytest.fixture
def db(monkeypatch):
    """Stub the scoped-config DB layer with an in-memory {(scope, id): {path: value}}."""
    rows: dict[tuple[Scope, int], dict[str, tuple[str | None, bool]]] = {}

    def _load(_conn, scope, scope_id):
        return rows.get((scope, scope_id), {})

    monkeypatch.setattr(
        "agento.framework.scoped_config.load_scoped_db_overrides", _load,
    )

    def put(scope: Scope, scope_id: int, path: str, value: str) -> None:
        rows.setdefault((scope, scope_id), {})[path] = (value, False)

    return put


def _resolve(**kwargs):
    defaults = dict(
        agent_view_id=7, workspace_id=3,
        harness_default=None, provider_default=None,
    )
    defaults.update(kwargs)
    return _resolve_harness_and_provider(object(), **defaults)


class TestLegacySingleAxisConfig:
    def test_env_provider_beats_the_config_json_harness_default(self, db, monkeypatch):
        """The bug the origin comparison exists to prevent: an operator carrying
        ``CONFIG__AGENT_VIEW__PROVIDER=codex`` would otherwise silently get the default
        harness (claude) plus a provider claude does not offer."""
        monkeypatch.setenv("CONFIG__AGENT_VIEW__PROVIDER", "codex")

        harness, provider = _resolve(harness_default="claude", provider_default="anthropic")

        assert (harness, provider) == ("codex", "openai")

    def test_stronger_scoped_harness_wins_over_weaker_legacy_provider(self, db):
        db(Scope.DEFAULT, 0, "agent_view/provider", "codex")     # pre-0.15 leftover
        db(Scope.AGENT_VIEW, 7, "agent_view/harness", "claude")  # migrated, stronger

        harness, provider = _resolve()

        # Never (claude, openai): the harness's own default provider is used.
        assert (harness, provider) == ("claude", "anthropic")

    def test_same_scope_legacy_provider_wins_ties(self, db):
        """Equal origins mean the operator only ever set the old key at that scope."""
        db(Scope.AGENT_VIEW, 7, "agent_view/provider", "codex")

        assert _resolve(harness_default="claude") == ("codex", "openai")

    def test_a_valid_provider_is_never_treated_as_legacy(self, db):
        """``anthropic`` is valid for ``claude``; it must be taken at face value even
        though provider names and harness names live in the same string space."""
        db(Scope.AGENT_VIEW, 7, "agent_view/harness", "claude")
        db(Scope.AGENT_VIEW, 7, "agent_view/provider", "anthropic")

        assert _resolve() == ("claude", "anthropic")

    def test_provider_naming_a_registered_harness_is_recognised_generically(self, db):
        """No literal list: registering a THIRD harness makes its id a legacy value too,
        with zero framework changes."""
        from pathlib import Path

        from agento.framework.harness import parse_harness_declarations, register_harness
        from agento.framework.module_loader import import_class

        fixtures = Path(__file__).resolve().parents[3] / "fixtures" / "modules"
        module_dir = fixtures / "fake_harness"
        register_builtin_harnesses()
        for decl in parse_harness_declarations(module_dir / "di.json", "fake_harness"):
            register_harness(decl.descriptor, import_class(module_dir, decl.class_path)())

        db(Scope.AGENT_VIEW, 7, "agent_view/provider", "fake")

        assert _resolve(harness_default="claude") == ("fake", "fake_local")

    def test_both_absent_yields_none_none(self, db):
        assert _resolve() == (None, None)

    def test_harness_only_falls_back_to_its_default_provider(self, db):
        db(Scope.AGENT_VIEW, 7, "agent_view/harness", "codex")
        assert _resolve() == ("codex", "openai")

    def test_a_genuinely_wrong_pair_raises_instead_of_guessing(self, db):
        """The old code silently fell back to Claude here."""
        db(Scope.AGENT_VIEW, 7, "agent_view/harness", "claude")
        db(Scope.AGENT_VIEW, 7, "agent_view/provider", "not_a_thing")

        with pytest.raises(ValueError, match="does not offer provider"):
            _resolve()

    def test_no_harness_literals_in_the_framework_resolution_path(self):
        """Guard against the regression this design keeps re-attracting.

        Two earlier attempts at this fallback special-cased "claude"/"codex" inside
        ``src/agento/framework/`` — the anti-pattern the whole refactor removes. Compare
        the AST (docstrings and comments dropped) so prose may still name them.
        """
        import ast
        from pathlib import Path

        module = ast.parse(
            Path(_resolve_harness_and_provider.__code__.co_filename).read_text()
        )
        func = next(
            n for n in ast.walk(module)
            if isinstance(n, ast.FunctionDef) and n.name == "_resolve_harness_and_provider"
        )
        literals = {
            n.value for n in ast.walk(func)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        # Drop the docstring, which is allowed to name them.
        literals.discard(ast.get_docstring(func))

        assert {"claude", "codex"}.isdisjoint(literals), (
            f"framework resolution hardcodes harness ids: {literals}"
        )
