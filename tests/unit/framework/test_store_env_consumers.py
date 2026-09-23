"""Every credential-store lookup goes through ``store_env`` — and the guard that keeps it that way.

The defect class: a store value read straight from ``os.environ``. Such a site silently
ignores the loaded store (the process would fall back to ambient values or to
nothing), and it is the shape that tempts a future ``os.environ.update`` fix — which would
put the store back into every child's environment.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agento.framework import crypto, database_config, store_env

_SRC = Path(__file__).resolve().parents[3] / "src" / "agento"
_ALLOWED = {"store_env.py"}
_STORE_LITERALS = ("MYSQL_", "AGENTO_ENCRYPTION_KEY", "CONFIG__")
_STORE_KEY_HELPERS = {"path_to_env_key", "_env_key", "_env_key_tool"}


@pytest.fixture(autouse=True)
def _reset():
    store_env.reset()
    yield
    store_env.reset()


def _load(**values: str) -> None:
    store_env.load(b"".join(f"{k}={v}\0".encode() for k, v in values.items()))


class TestConsumersResolveFromTheStore:
    def test_database_config_resolves_with_an_empty_environ(self, monkeypatch):
        for name in ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
            monkeypatch.delenv(name, raising=False)
        _load(MYSQL_HOST="db", MYSQL_PORT="3307", MYSQL_DATABASE="d", MYSQL_USER="u", MYSQL_PASSWORD="p")

        cfg = database_config.DatabaseConfig.from_env()

        assert (cfg.mysql_host, cfg.mysql_port, cfg.mysql_database) == ("db", 3307, "d")
        assert (cfg.mysql_user, cfg.mysql_password) == ("u", "p")

    def test_the_encryption_key_resolves_with_an_empty_environ(self, monkeypatch):
        monkeypatch.delenv("AGENTO_ENCRYPTION_KEY", raising=False)
        _load(AGENTO_ENCRYPTION_KEY="passphrase")

        assert crypto.decrypt(crypto.encrypt("secret")) == "secret"

    def test_an_ambient_config_override_still_wins_at_the_env_level(self, monkeypatch):
        # The 3-level fallback's ENV level must keep working for non-store deployments.
        from agento.framework.config_resolver import resolve_field

        monkeypatch.setenv("CONFIG__DEMO__TOKEN", "from-environ")

        rv = resolve_field("demo", "token", {"type": "string"}, {}, {})

        assert (rv.value, rv.source) == ("from-environ", "env")

    def test_a_stored_config_override_resolves_at_the_env_level(self, monkeypatch):
        from agento.framework.config_resolver import resolve_field

        monkeypatch.delenv("CONFIG__DEMO__TOKEN", raising=False)
        _load(CONFIG__DEMO__TOKEN="from-store")

        rv = resolve_field("demo", "token", {"type": "string"}, {}, {})

        assert (rv.value, rv.source) == ("from-store", "env")


class TestNoSiteBypassesStoreEnv:
    """Structural guard: bans the SHAPE, not a word.

    A bare ``rg os.environ`` would fire on every legitimate non-store env read in the
    repo, so this walks the AST and flags only ``os.environ``/``os.getenv`` lookups whose
    key is a credential-store key — a store literal, or the result of one of the helpers
    that builds a ``CONFIG__*`` name.
    """

    def _offenders(self) -> list[str]:
        found = []
        for path in _SRC.rglob("*.py"):
            if path.name in _ALLOWED or "/tests/" in path.as_posix():
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                key = _store_key_arg(node)
                if key is not None:
                    found.append(f"{path.relative_to(_SRC.parent.parent)}:{node.lineno} ({key})")
        return found

    def test_no_production_site_reads_a_store_key_from_os_environ(self):
        assert self._offenders() == []


def _store_key_arg(node: ast.AST) -> str | None:
    """The store key this node looks up in ``os.environ``/``os.getenv``, if it does."""
    arg = None
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        arg = node.slice
    elif isinstance(node, ast.Call):
        fn = node.func
        if (isinstance(fn, ast.Attribute) and fn.attr == "get" and _is_os_environ(fn.value)) or (isinstance(fn, ast.Attribute) and fn.attr == "getenv" and _is_os(fn.value)):
            arg = node.args[0] if node.args else None
    if arg is None:
        return None
    if (
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and arg.value.startswith(_STORE_LITERALS)
    ):
        return arg.value
    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id in _STORE_KEY_HELPERS:
        return f"{arg.func.id}(...)"
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and _is_os(node.value)


def _is_os(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "os"
