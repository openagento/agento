"""Tests for CLI command shortcut registry."""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

import pytest

import agento
from agento.framework.commands import (
    _segment_code,
    clear,
    get_commands,
    get_shortcuts,
    is_valid_shortcut,
    register_command,
    resolve_shortcut,
)
from agento.framework.module_loader import import_class


class _StubCommand:
    def __init__(self, name: str, shortcut: str = ""):
        self._name = name
        self._shortcut = shortcut

    @property
    def name(self) -> str:
        return self._name

    @property
    def shortcut(self) -> str:
        return self._shortcut

    @property
    def help(self) -> str:
        return f"Help for {self._name}"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


class TestShortcutRegistration:
    def test_command_with_shortcut_registers_in_shortcuts(self):
        cmd = _StubCommand("setup:upgrade", "se:up")
        register_command(cmd)

        assert get_shortcuts() == {"se:up": "setup:upgrade"}
        assert "setup:upgrade" in get_commands()

    def test_command_with_empty_shortcut_not_in_shortcuts(self):
        cmd = _StubCommand("consumer", "")
        register_command(cmd)

        assert get_shortcuts() == {}
        assert "consumer" in get_commands()

    def test_shortcut_conflict_logs_warning_first_wins(self, caplog):
        cmd1 = _StubCommand("first:cmd", "fc")
        cmd2 = _StubCommand("second:cmd", "fc")

        register_command(cmd1)
        with caplog.at_level(logging.WARNING):
            register_command(cmd2)

        assert get_shortcuts() == {"fc": "first:cmd"}
        assert "already registered" in caplog.text

    def test_clear_resets_both_registries(self):
        register_command(_StubCommand("a:cmd", "ac"))
        assert get_commands()
        assert get_shortcuts()

        clear()

        assert get_commands() == {}
        assert get_shortcuts() == {}

    def test_get_shortcuts_returns_copy(self):
        register_command(_StubCommand("x:cmd", "xc"))
        shortcuts = get_shortcuts()
        shortcuts["injected"] = "bad"

        assert "injected" not in get_shortcuts()

    def test_get_commands_returns_copy(self):
        register_command(_StubCommand("y:cmd", "yc"))
        commands = get_commands()
        commands["injected"] = None

        assert "injected" not in get_commands()

    def test_multiple_commands_with_different_shortcuts(self):
        register_command(_StubCommand("config:set", "co:se"))
        register_command(_StubCommand("config:get", "co:ge"))
        register_command(_StubCommand("consumer", ""))

        assert get_shortcuts() == {"co:se": "config:set", "co:ge": "config:get"}
        assert len(get_commands()) == 3


def _all_real_commands() -> list:
    """Collect every registered command (framework + core modules) without a DB.

    Mirrors how the CLI and bootstrap populate the registry, so the compliance
    test sees the same (name, shortcut) pairs a user would.
    """
    from agento.framework.cli import _register_framework_commands

    clear()
    _register_framework_commands()
    instances = list(get_commands().values())

    modules_root = Path(agento.__file__).parent / "modules"
    for di_path in sorted(modules_root.glob("*/di.json")):
        data = json.loads(di_path.read_text())
        for decl in data.get("commands", []):
            cls = import_class(di_path.parent, decl["class"])
            instances.append(cls())
    clear()
    return instances


def _derived_shortcut(name: str) -> str:
    return ":".join(_segment_code(seg, is_first=i == 0) for i, seg in enumerate(name.split(":")))


class TestShortcutPatternCompliance:
    """Guards: every shipped shortcut follows the documented derivation rule."""

    def test_every_shortcut_is_derivable(self):
        offenders = [
            (cmd.name, cmd.shortcut)
            for cmd in _all_real_commands()
            if cmd.shortcut and not is_valid_shortcut(cmd.name, cmd.shortcut)
        ]
        assert offenders == [], f"Shortcuts violating the documented pattern: {offenders}"

    def test_shortcuts_are_globally_unique(self):
        seen: dict[str, str] = {}
        collisions: list[tuple[str, str, str]] = []
        for cmd in _all_real_commands():
            if not cmd.shortcut:
                continue
            if cmd.shortcut in seen:
                collisions.append((cmd.shortcut, seen[cmd.shortcut], cmd.name))
            else:
                seen[cmd.shortcut] = cmd.name
        assert collisions == [], f"Duplicate shortcuts: {collisions}"


    def test_every_namespaced_command_has_a_shortcut(self):
        """Exempt only a pair whose derived code collides (publish-comments / publish-changes)."""
        commands = _all_real_commands()
        derived = Counter(_derived_shortcut(cmd.name) for cmd in commands)
        missing = [
            cmd.name
            for cmd in commands
            if ":" in cmd.name and not cmd.shortcut and not getattr(cmd, "hidden", False)
            and derived[_derived_shortcut(cmd.name)] == 1
        ]
        assert missing == [], f"Commands without a shortcut: {missing}"

    def test_shortcut_routes_like_its_command(self):
        """The host routes by the raw argv string, before any shortcut is resolved."""
        from agento.framework import cli

        sets = {
            "_LOCAL_COMMANDS": cli._LOCAL_COMMANDS,
            "_LOCAL_MODULE_COMMANDS": cli._LOCAL_MODULE_COMMANDS,
            "_INTERACTIVE_COMMANDS": cli._INTERACTIVE_COMMANDS,
            "_MAYBE_INTERACTIVE_COMMANDS": cli._MAYBE_INTERACTIVE_COMMANDS,
        }
        drift = [
            (set_name, cmd.name, cmd.shortcut)
            for cmd in _all_real_commands()
            if cmd.shortcut
            for set_name, names in sets.items()
            if (cmd.name in names) != (cmd.shortcut in names)
        ]
        assert drift == [], f"Shortcut routed differently from its command: {drift}"


class TestIsValidShortcut:
    @pytest.mark.parametrize(
        "name,shortcut",
        [
            ("config:set", "co:se"),
            ("workspace:build", "wo:bu"),
            ("workspace:build-status", "wo:bs"),
            ("token:set-priority", "to:sp"),
            ("token:mark-error", "to:me"),
            ("token:reset", "to:res"),
            ("tool:list", "tl:li"),
            ("agent_view:runtime", "av:ru"),
            ("agent_view:identity:show", "av:id:sh"),
            ("run", "ru"),
            ("consumer", ""),
        ],
    )
    def test_valid(self, name, shortcut):
        assert is_valid_shortcut(name, shortcut)

    @pytest.mark.parametrize(
        "name,shortcut",
        [
            ("workspace:build", "ws:b"),
            ("workspace:build-status", "ws:bs"),
            ("token:reset", "to:rs"),
            ("agent_view:runtime", "av:rt"),
            ("config:set", "c:se"),
            ("config:set", "co:se:x"),
        ],
    )
    def test_invalid(self, name, shortcut):
        assert not is_valid_shortcut(name, shortcut)


class TestResolveShortcut:
    def test_resolves_known_shortcut(self):
        register_command(_StubCommand("config:set", "co:se"))

        assert resolve_shortcut("co:se") == "config:set"

    def test_returns_input_for_unknown_shortcut(self):
        assert resolve_shortcut("no:such") == "no:such"

    def test_returns_full_command_name_unchanged(self):
        register_command(_StubCommand("config:set", "co:se"))

        assert resolve_shortcut("config:set") == "config:set"

    def test_returns_empty_string_unchanged(self):
        assert resolve_shortcut("") == ""
