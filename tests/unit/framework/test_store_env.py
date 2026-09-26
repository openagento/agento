"""The credential store is loaded in-process, never through the environment.

The defect this guards is the one the V0 design exists to close: a value that lands in
``os.environ`` is inherited by every later child at ``execve`` and sits in that child's
peer-readable ``/proc/<pid>/environ`` for its whole life.
"""
from __future__ import annotations

import os

import pytest

from agento.framework import store_env


@pytest.fixture(autouse=True)
def _reset():
    store_env.reset()
    yield
    store_env.reset()


class TestParsing:
    def test_values_resolve_after_a_load(self):
        store_env.load(b"MYSQL_HOST=db\0AGENTO_ENCRYPTION_KEY=s3cret\0")

        assert store_env.get("MYSQL_HOST") == "db"
        assert store_env.get("AGENTO_ENCRYPTION_KEY") == "s3cret"

    def test_os_environ_is_untouched(self):
        store_env.load(b"MYSQL_PASSWORD=hunter2\0")

        assert "MYSQL_PASSWORD" not in os.environ
        assert store_env.get("MYSQL_PASSWORD") == "hunter2"

    def test_a_record_without_an_equals_is_fatal(self):
        with pytest.raises(ValueError):
            store_env.load(b"MYSQL_HOST=db\0GARBAGE\0")

    def test_a_value_containing_a_newline_survives(self):
        # Why NUL and not KEY=value lines: a newline in a password would otherwise
        # split into a forged second assignment.
        store_env.load(b"CONFIG__X__Y=line1\nline2\0")

        assert store_env.get("CONFIG__X__Y") == "line1\nline2"

    def test_a_value_containing_an_equals_survives(self):
        store_env.load(b"MYSQL_PASSWORD=a=b=c\0")

        assert store_env.get("MYSQL_PASSWORD") == "a=b=c"

    def test_a_value_containing_quotes_survives(self):
        store_env.load(b"""CONFIG__A__B='x' "y" $(id)\0""")

        assert store_env.get("CONFIG__A__B") == """'x' "y" $(id)"""

    def test_a_trailing_nul_does_not_produce_an_empty_record(self):
        store_env.load(b"MYSQL_HOST=db\0")

        assert store_env.get("MYSQL_HOST") == "db"


class TestFailClosed:
    def test_nothing_is_loaded_when_a_record_is_malformed(self):
        with pytest.raises(ValueError):
            store_env.load(b"MYSQL_HOST=db\0GARBAGE\0")

        # Fail closed: a partial load would run the command with half a configuration.
        assert store_env.get("MYSQL_HOST") is None


class TestLookupOrder:
    def test_the_store_wins_over_os_environ(self, monkeypatch):
        monkeypatch.setenv("MYSQL_HOST", "from-environ")
        store_env.load(b"MYSQL_HOST=from-store\0")

        assert store_env.get("MYSQL_HOST") == "from-store"

    def test_os_environ_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("CONFIG__JIRA__URL", "from-environ")

        assert store_env.get("CONFIG__JIRA__URL") == "from-environ"

    def test_environ_view_merges_both(self, monkeypatch):
        monkeypatch.setenv("CONFIG__A__B", "ambient")
        store_env.load(b"CONFIG__C__D=stored\0")

        merged = store_env.environ()

        assert merged["CONFIG__A__B"] == "ambient"
        assert merged["CONFIG__C__D"] == "stored"

    def test_the_environ_view_is_read_only(self):
        store_env.load(b"MYSQL_HOST=db\0")

        with pytest.raises(TypeError):
            store_env.environ()["MYSQL_HOST"] = "mutated"

        assert store_env.get("MYSQL_HOST") == "db"
        assert "MYSQL_HOST" not in os.environ
