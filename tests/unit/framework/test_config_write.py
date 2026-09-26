"""config_write: the one write path, and the web form that must prove a field is not a secret."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agento.framework import bootstrap
from agento.framework.config_write import ConfigWriteError, save_config, validate_config_write, write_config


@pytest.fixture
def modules(tmp_path, monkeypatch):
    core, user = tmp_path / "core", tmp_path / "user"
    fake = core / "fake"
    fake.mkdir(parents=True)
    user.mkdir()
    (fake / "module.json").write_text(json.dumps({
        "name": "fake", "version": "1", "description": "d",
        "tools": [
            {"type": "mcp", "name": "fake_tool", "toolset": "fake", "requires": "fake_switch",
             "fields": {"host": {"type": "string"}, "pass": {"type": "obscure"}, "raw": "string"}},
            {"type": "mcp", "name": "fake_switch", "toolset": "fake"},
        ],
    }))
    (fake / "system.json").write_text(json.dumps({
        "limit": {"type": "integer", "label": "Limit"},
        "mode": {"type": "select", "options": [{"value": "a"}, {"value": "b"}]},
        "secret": {"type": "obscure"},
        "hidden": {"type": "string", "access": "toolbox_only"},
        "identity/ssh_private_key": {"type": "obscure"},
        "flat": "string",
    }))
    broken = core / "broken"
    broken.mkdir()
    (broken / "module.json").write_text(json.dumps({"name": "broken", "version": "1", "description": "d"}))
    (broken / "system.json").write_text("{not json")
    monkeypatch.setattr(bootstrap, "CORE_MODULES_DIR", str(core))
    monkeypatch.setattr(bootstrap, "USER_MODULES_DIR", str(user))
    return core


def _web(path, value="1", scope="default", scope_id=0):
    validate_config_write(None, path, value, scope, scope_id, allow_secret=False)


@pytest.mark.parametrize("path", [
    "fake/secret",
    "fake/hidden",
    "fake/identity/ssh_private_key",
    "fake/flat",
    "fake/tools/fake_tool/pass",
    "fake/tools/fake_tool/raw",
    "fake/tools/fake_tool/undeclared",
    "fake/tools/no_tool/host",
    "fake/no_such_field",
    "broken/anything",
    "nosuchmodule/x",
])
def test_web_form_refuses_what_it_cannot_prove_non_secret(modules, path):
    with pytest.raises(ConfigWriteError, match="config:set"):
        _web(path)


def test_web_form_accepts_a_plain_schema_field(modules):
    _web("fake/limit", "5")
    _web("fake/tools/fake_tool/host", "db.local")


def test_web_form_still_validates_the_value(modules):
    with pytest.raises(ConfigWriteError, match="Allowed values: a, b"):
        _web("fake/mode", "c")


def test_gate_key_for_declared_tools_and_requires_keys(modules):
    _web("tools/fake_tool/is_enabled", "1", "agent_view", 3)
    _web("tools/fake_switch/is_enabled", "0")
    with pytest.raises(ConfigWriteError, match="declares"):
        _web("tools/other_tool/is_enabled")
    with pytest.raises(ConfigWriteError, match="0 or 1"):
        _web("tools/fake_tool/is_enabled", "yes")


def test_cli_form_keeps_refusing_the_gate_key(modules):
    with pytest.raises(ConfigWriteError, match="Module 'tools' not found"):
        validate_config_write(None, "tools/fake_tool/is_enabled", "1", "default", 0, allow_secret=True)


def test_cli_form_may_write_a_secret_field(modules):
    validate_config_write(None, "fake/secret", "x", "default", 0, allow_secret=True)


@patch("agento.framework.event_manager.get_event_manager")
@patch("agento.framework.config_dependents.set_config_with_dependents", return_value=(False, [("a/b", "c")]))
def test_write_config_commits_and_dispatches(mock_set, mock_events):
    conn = MagicMock()
    assert write_config(conn, "fake/limit", "5", scope="default", scope_id=0) == (False, [("a/b", "c")])
    conn.commit.assert_called_once()
    assert mock_events.return_value.dispatch.call_args.args[0] == "config_save_after"


@patch("agento.framework.config_dependents.set_config_with_dependents")
def test_save_config_writes_nothing_when_refused(mock_set, modules):
    conn = MagicMock()
    with pytest.raises(ConfigWriteError):
        save_config(conn, "fake/secret", "x", scope="default", scope_id=0, allow_secret=False)
    mock_set.assert_not_called()
    conn.begin.assert_not_called()
