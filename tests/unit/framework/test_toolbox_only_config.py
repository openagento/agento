"""`access: toolbox_only` and `allowEnv: false` — enforced on the Python side.

The toolbox is the only container holding secrets. These tests pin the two rules that
keep it that way: Python never resolves (and never decrypts) a toolbox-only field, and a
field that opted out of the ENV source cannot be supplied through the process env.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agento.framework.config_resolver import ScopedConfigService, resolve_field
from agento.framework.config_schema import (
    ToolboxOnlyConfigError,
    clear_restricted_fields,
    env_allowed,
    is_toolbox_only,
    remember_restricted_fields,
)

_SECRET = {"type": "obscure", "access": "toolbox_only", "allowEnv": False}
_NO_ENV = {"type": "string", "allowEnv": False}
_PLAIN = {"type": "string"}


def _manifest(config: dict) -> SimpleNamespace:
    """The shape `get_manifests()` returns: objects with `.name` and `.config`."""
    return SimpleNamespace(name="outlook", config=config)


class TestFieldMetadata:
    def test_is_toolbox_only_reads_the_access_key(self):
        assert is_toolbox_only(_SECRET) is True
        assert is_toolbox_only(_PLAIN) is False
        assert is_toolbox_only({"access": "anything_else"}) is False

    def test_env_is_allowed_unless_explicitly_refused(self):
        assert env_allowed(_PLAIN) is True
        assert env_allowed({}) is True
        assert env_allowed(_NO_ENV) is False


class TestResolveField:
    def test_a_toolbox_only_field_resolves_to_nothing_without_decrypting(self, monkeypatch):
        def explode(*_a, **_k):
            raise AssertionError("the decryptor must not be called for a toolbox-only field")

        monkeypatch.setattr("agento.framework.config_resolver.get_encryptor", explode)
        rv = resolve_field(
            "outlook", "outlook_client_secret", _SECRET, {},
            {"outlook/outlook_client_secret": ("ciphertext", True)},
        )
        assert rv.value is None
        assert rv.source == "toolbox_only"

    def test_a_toolbox_only_field_ignores_the_environment(self, monkeypatch):
        monkeypatch.setenv("CONFIG__OUTLOOK__OUTLOOK_CLIENT_SECRET", "from-env")
        rv = resolve_field("outlook", "outlook_client_secret", _SECRET, {}, {})
        assert rv.value is None
        assert rv.source == "toolbox_only"

    def test_allow_env_false_skips_only_the_env_step(self, monkeypatch):
        monkeypatch.setenv("CONFIG__OUTLOOK__SOME_FIELD", "from-env")
        rv = resolve_field(
            "outlook", "some_field", _NO_ENV, {}, {"outlook/some_field": ("from-db", False)}
        )
        assert rv.value == "from-db"
        assert rv.source == "db"

    def test_allow_env_false_still_falls_back_to_config_json(self, monkeypatch):
        monkeypatch.setenv("CONFIG__OUTLOOK__SOME_FIELD", "from-env")
        rv = resolve_field("outlook", "some_field", _NO_ENV, {"some_field": "from-json"}, {})
        assert rv.value == "from-json"
        assert rv.source == "config.json"

    def test_a_plain_field_still_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("CONFIG__OUTLOOK__SOME_FIELD", "from-env")
        rv = resolve_field("outlook", "some_field", _PLAIN, {}, {})
        assert rv.value == "from-env"
        assert rv.source == "env"


class TestScopedConfigServiceGet:
    def test_a_direct_get_of_a_toolbox_only_path_raises(self, monkeypatch):
        monkeypatch.setattr(
            "agento.framework.bootstrap.get_manifests",
            lambda: [_manifest({"outlook_client_secret": _SECRET})],
        )
        svc = ScopedConfigService.__new__(ScopedConfigService)
        svc._overrides = {"outlook/outlook_client_secret": ("ciphertext", True)}
        svc._scope_overrides = {}
        with pytest.raises(ToolboxOnlyConfigError) as ei:
            svc.get("outlook/outlook_client_secret")
        # The message must name the path — a caller reads it in a traceback, not a debugger.
        assert "outlook/outlook_client_secret" in str(ei.value)

    def test_a_normal_path_is_unaffected(self, monkeypatch):
        monkeypatch.setattr(
            "agento.framework.bootstrap.get_manifests",
            lambda: [_manifest({"poll_top": _PLAIN})],
        )
        svc = ScopedConfigService.__new__(ScopedConfigService)
        svc._overrides = {"outlook/poll_top": ("10", False)}
        svc._scope_overrides = {}
        assert svc.get("outlook/poll_top") == "10"


class TestResolveAllSkipsToolboxOnly:
    """`resolve_all()` walks EVERY declared path. It is called by the workspace builder on every
    run, so a raise there breaks unrelated jobs; a toolbox-only path is simply not Python's to
    resolve and must be absent from the result, exactly like a value that is not set."""

    def _svc(self, monkeypatch, config, overrides):
        monkeypatch.setattr(
            "agento.framework.bootstrap.get_manifests", lambda: [_manifest(config)]
        )
        svc = ScopedConfigService.__new__(ScopedConfigService)
        svc._overrides = overrides
        svc._scope_overrides = {}
        return svc

    def test_a_toolbox_only_path_is_omitted_instead_of_raising(self, monkeypatch):
        svc = self._svc(
            monkeypatch,
            {"outlook_client_secret": _SECRET, "poll_top": _PLAIN},
            {"outlook/outlook_client_secret": ("ciphertext", True), "outlook/poll_top": ("10", False)},
        )
        out = svc.resolve_all()
        assert "outlook/outlook_client_secret" not in out
        assert out["outlook/poll_top"] == "10"

    def test_a_toolbox_only_env_key_cannot_smuggle_the_value_back_in(self, monkeypatch):
        monkeypatch.setenv("CONFIG__OUTLOOK__OUTLOOK_CLIENT_SECRET", "leaked")
        svc = self._svc(monkeypatch, {"outlook_client_secret": _SECRET}, {})
        assert "outlook/outlook_client_secret" not in svc.resolve_all()


class TestSchemaAbsenceFailsClosed:
    """The security metadata is the only thing between Python and a decrypted secret, so its
    ABSENCE must never read as "unrestricted". A re-bootstrap that fails part-way, a module that
    was disabled, or a manifest lookup that raises all leave the live registry unable to answer —
    and every one of them is reachable while the consumer keeps serving (it catches a failed
    reload and continues)."""

    def _svc(self, overrides):
        svc = ScopedConfigService.__new__(ScopedConfigService)
        svc._overrides = overrides
        svc._scope_overrides = {}
        return svc

    @pytest.fixture(autouse=True)
    def _remembered(self):
        clear_restricted_fields()
        remember_restricted_fields("outlook", {"outlook_client_secret": _SECRET})
        yield
        clear_restricted_fields()

    def test_an_empty_manifest_registry_still_refuses_the_secret(self, monkeypatch):
        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", list)
        svc = self._svc({"outlook/outlook_client_secret": ("plaintext-proof", False)})
        with pytest.raises(ToolboxOnlyConfigError):
            svc.get("outlook/outlook_client_secret")

    def test_a_manifest_lookup_that_raises_still_refuses_the_secret(self, monkeypatch):
        def _boom():
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", _boom)
        svc = self._svc({"outlook/outlook_client_secret": ("plaintext-proof", False)})
        with pytest.raises(ToolboxOnlyConfigError):
            svc.get("outlook/outlook_client_secret")

    def test_env_is_still_refused_for_an_allow_env_false_field_without_manifests(self, monkeypatch):
        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", list)
        monkeypatch.setenv("CONFIG__OUTLOOK__OUTLOOK_CLIENT_SECRET", "leaked")
        with pytest.raises(ToolboxOnlyConfigError):
            self._svc({}).get("outlook/outlook_client_secret")

    def test_a_slash_keyed_restricted_field_is_found(self, monkeypatch):
        remember_restricted_fields("agent_view", {"identity/ssh_private_key": _SECRET})
        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", list)
        svc = self._svc({"agent_view/identity/ssh_private_key": ("key", True)})
        with pytest.raises(ToolboxOnlyConfigError):
            svc.get("agent_view/identity/ssh_private_key")

    def test_an_encrypted_value_is_not_decrypted_when_nothing_is_known(self, monkeypatch):
        """A process that never completed one bootstrap knows nothing about any field. An
        encrypted row is the storage form of every secret, so it is refused rather than
        decrypted on a guess."""
        clear_restricted_fields()
        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", list)
        with pytest.raises(ToolboxOnlyConfigError):
            self._svc({"somemod/anything": ("ciphertext", True)}).get("somemod/anything")

    def test_a_plaintext_value_still_resolves_when_nothing_is_known(self, monkeypatch):
        """The backstop must not break ordinary config: a plaintext row is not a secret."""
        clear_restricted_fields()
        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", list)
        assert self._svc({"agent_view/harness": ("claude", False)}).get("agent_view/harness") == "claude"

    def test_resolve_all_omits_the_unreadable_value_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr("agento.framework.bootstrap.get_manifests", list)
        svc = self._svc({
            "outlook/outlook_client_secret": ("ciphertext", True),
            "outlook/poll_top": ("10", False),
        })
        out = svc.resolve_all()
        assert "outlook/outlook_client_secret" not in out
        assert out["outlook/poll_top"] == "10"
