"""get_resolved_fields never resolves a secret: source and is_set come from presence (SEC-2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agento.framework.admin.data import ModuleSchema, get_resolved_fields
from agento.framework.scoped_config import Scope

FIELDS = {
    "plain": {"type": "string"},
    "secret": {"type": "obscure"},
    "hidden": {"type": "string", "access": "toolbox_only"},
    "identity/ssh_private_key": {"type": "obscure"},
    "dbonly": {"type": "obscure", "allowEnv": False},
    "flag": {"type": "boolean"},
    "obj": {"type": "json"},
}
TOOLS = {"t": {"pass": {"type": "obscure"}, "on": {"type": "boolean"}}}


def _conn():
    conn, cur = MagicMock(), MagicMock()
    cur.fetchone.return_value = {"workspace_id": 1}
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


def _fields(merged=None, local=None, env=None, defaults=None, scope=Scope.AGENT_VIEW, scope_id=42):
    schema = ModuleSchema(name="m", fields=FIELDS, tools=TOOLS, module_path=Path("/nonexistent/m"))
    env = env or {}
    with patch("agento.framework.admin.data.get_module_schemas", return_value=[schema]), \
         patch("agento.framework.admin.data.read_config_defaults", return_value=defaults or {}), \
         patch("agento.framework.scoped_config.build_scoped_overrides", return_value=merged or {}), \
         patch("agento.framework.scoped_config.load_scoped_db_overrides", return_value=local or {}), \
         patch.dict("os.environ", env, clear=True), \
         patch("agento.framework.config_resolver.get_encryptor", side_effect=AssertionError("decrypted")), \
         patch("agento.framework.config_resolver.ScopedConfigService.resolve_all",
               side_effect=AssertionError("resolve_all decrypts every secret in scope")):
        return {f.path: f for f in get_resolved_fields(_conn(), "m", scope, scope_id)}


SECRETS = ["m/secret", "m/hidden", "m/identity/ssh_private_key", "m/tools/t/pass"]


@pytest.mark.parametrize("path", SECRETS)
def test_a_stored_secret_is_set_but_has_no_value(path):
    row = {path: ("ciphertext", True)}
    f = _fields(merged=row, local=row)[path]
    assert (f.secret, f.is_set, f.value, f.source, f.display_value) == (True, True, None, "db", "****")


@pytest.mark.parametrize("path", SECRETS)
def test_a_secret_set_at_a_parent_scope_is_inherited(path):
    f = _fields(merged={path: ("ciphertext", True)}, local={})[path]
    assert (f.source, f.is_set, f.value) == ("db:inherited", True, None)


def test_an_unset_secret_and_a_json_default():
    fields = _fields(defaults={"secret": "from-json"})
    assert (fields["m/secret"].source, fields["m/secret"].is_set) == ("json", True)
    assert fields["m/secret"].value is None
    assert (fields["m/hidden"].source, fields["m/hidden"].is_set, fields["m/hidden"].display_value) == ("none", False, "")


@pytest.mark.parametrize(("path", "key"), [("m/secret", "CONFIG__M__SECRET"),
                                           ("m/tools/t/pass", "CONFIG__M__TOOLS__T__PASS")])
def test_env_wins_over_db_for_a_secret_with_no_value_read(path, key):
    env = {key: "plain-env-secret"}
    for merged in ({}, {path: ("ciphertext", True)}):
        f = _fields(merged=merged, local=merged, env=env)[path]
        assert (f.source, f.is_set, f.value) == ("env", True, None)


def test_allow_env_false_ignores_env():
    f = _fields(env={"CONFIG__M__DBONLY": "x"})["m/dbonly"]
    assert (f.source, f.is_set) == ("none", False)


def test_a_json_default_is_shown_as_json_text():
    # The panel's switch reads "true"; Python's str(True) is "True" and showed an enabled default as off.
    fields = _fields(defaults={"flag": True, "obj": {"a": [1]}, "tools": {"t": {"on": False}}})
    assert [fields[p].value for p in ("m/flag", "m/obj", "m/tools/t/on")] == ["true", '{"a": [1]}', "false"]


def test_a_plain_field_keeps_its_value():
    row = {"m/plain": ("hello", False)}
    f = _fields(merged=row, local=row)["m/plain"]
    assert (f.secret, f.is_set, f.value, f.source) == (False, True, "hello", "db")


class TestEditorKeepsAnUntouchedSecret:
    """The editor never holds the stored secret, so Save on an empty box must not write ''."""

    def _screen(self, *, secret, value):
        from unittest.mock import MagicMock

        from agento.framework.admin.screens.config import ConfigFieldEditorScreen

        s = MagicMock(spec=ConfigFieldEditorScreen)
        s._field = MagicMock(secret=secret, field_type="obscure" if secret else "string", max_length=None)
        s._get_value.return_value = value
        s._validate.return_value = None
        ConfigFieldEditorScreen._save(s)
        return s

    def test_empty_secret_is_not_saved(self):
        s = self._screen(secret=True, value="")
        s._do_save.assert_not_called()
        s.dismiss.assert_called_once_with(False)

    def test_new_secret_is_saved(self):
        self._screen(secret=True, value="n3w")._do_save.assert_called_once_with("n3w")

    def test_empty_plain_field_is_saved(self):
        self._screen(secret=False, value="")._do_save.assert_called_once_with("")
