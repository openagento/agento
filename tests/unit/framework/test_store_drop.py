"""The store is loaded after the privilege drop and reaches no child.

Two defects this guards, both invisible once they regress:

* Revision 2 of the design loaded the payload with ``os.environ.update``. That does not
  rewrite the current process's ``/proc/<pid>/environ`` (fixed at ``execve``), but it does
  change what every later ``execve`` hands its children — and the framework builds child
  environments from ``{**os.environ, …}`` in four places. The assertion that catches it is
  on a CHILD, not on the parent.
* Revision 3 handed the store to the dropped process on a file descriptor. ``execve``
  resets ``PR_SET_DUMPABLE``, and the containers have no yama ``ptrace_scope``, so a
  same-uid peer could attach during Python's startup and read it off that descriptor.
  ``drop.py`` closes that by dropping IN-PROCESS: no exec follows the drop.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agento.framework import store_env

DROP_PY = (
    Path(__file__).resolve().parents[3] / "src/agento/framework/docker/cron/drop.py"
)

_SHAPES = {
    "MYSQL_HOST": "db.internal",
    "MYSQL_PASSWORD": "hunter2",
    "AGENTO_ENCRYPTION_KEY": "passphrase",
    "CONFIG__DEMO__TOKEN": "tok",
}


@pytest.fixture(autouse=True)
def _reset():
    store_env.reset()
    yield
    store_env.reset()


def _payload() -> bytes:
    return b"".join(f"{k}={v}\0".encode() for k, v in _SHAPES.items())


class TestLoading:
    def test_the_payload_becomes_the_store(self):
        store_env.load(_payload())

        assert store_env.get("MYSQL_PASSWORD") == "hunter2"

    def test_a_malformed_payload_aborts(self):
        with pytest.raises(ValueError):
            store_env.load(b"MYSQL_HOST=db\0GARBAGE\0")

    def test_the_store_is_absent_from_os_environ(self):
        # `tests/integration/conftest.py` sets AGENTO_ENCRYPTION_KEY at import time for
        # the MySQL suites, so assert the LOAD adds nothing rather than that the process
        # environment is clean.
        before = dict(os.environ)

        store_env.load(_payload())

        for name in _SHAPES:
            assert os.environ.get(name) == before.get(name)


class TestNoChildInheritsTheStore:
    def test_a_spawned_child_receives_none_of_the_store(self):
        before = dict(os.environ)
        store_env.load(_payload())

        child = subprocess.run(
            [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ},  # the shape every leaking call site in the repo uses
        )
        child_env = json.loads(child.stdout)

        leaked = sorted(
            name for name in _SHAPES
            if child_env.get(name, before.get(name)) != before.get(name)
        )
        assert leaked == [], f"store shapes reached the child: {leaked}"


class TestTheDropOrder:
    """`drop.py` runs as root; these assert its shape, since the real drop needs uid 0."""

    def _tree(self) -> ast.Module:
        return ast.parse(DROP_PY.read_text())

    def test_the_store_is_read_before_the_drop(self):
        main = next(
            n for n in self._tree().body
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        body = main.body
        source = ast.unparse(ast.Module(body=body, type_ignores=[]))

        assert source.index("STORE_FILE") < source.index("_drop_to")

    def test_the_framework_is_imported_only_after_the_drop(self):
        """An import before the drop would run framework code as root."""
        source = DROP_PY.read_text()
        drop_at = source.index("_drop_to(AGENT_USER)")

        for imported in ("process_hardening", "store_env", "agento.framework.cli"):
            assert source.index(imported, drop_at) > drop_at
            assert imported not in source[: source.index("def _drop_to")]

    def test_the_drop_sets_every_id(self):
        source = DROP_PY.read_text()

        for call in ("setgroups", "setresgid", "setresuid"):
            assert f"os.{call}(" in source
        # setresuid clears the saved id too, so there is no way back to uid 0.
        assert "sys.exit" in source
