"""`make_non_dumpable` — withdraw ptrace-mode access to a framework process.

The kernel effect cannot be asserted portably (the dev machines are darwin), so the
tests drive the libc seam and assert the syscall arguments and the failure policy.
The live check is recorded in the loop's VERIFICATION-live.md.
"""
from __future__ import annotations

import sys

import pytest

from agento.framework import process_hardening
from agento.framework.process_hardening import PR_SET_DUMPABLE, make_non_dumpable


class _FakeLibc:
    def __init__(self, rc=0, raises=None):
        self.rc = rc
        self.raises = raises
        self.calls: list[tuple] = []

    def prctl(self, *args):
        self.calls.append(args)
        if self.raises:
            raise self.raises
        return self.rc


class TestMakeNonDumpable:
    def test_asks_the_kernel_to_clear_the_dumpable_flag(self, monkeypatch):
        libc = _FakeLibc()
        monkeypatch.setattr(process_hardening, "_load_libc", lambda: libc)
        assert make_non_dumpable() is True
        # PR_SET_DUMPABLE with 0 — anything else would leave the process readable.
        assert libc.calls == [(PR_SET_DUMPABLE, 0, 0, 0, 0)]

    def test_the_constant_is_the_kernel_value(self):
        # linux/prctl.h: PR_SET_DUMPABLE 4. A wrong number silently sets some
        # OTHER process attribute, so pin it.
        assert PR_SET_DUMPABLE == 4

    def test_a_refused_syscall_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(process_hardening, "_load_libc", lambda: _FakeLibc(rc=-1))
        assert make_non_dumpable() is False

    @pytest.mark.parametrize("exc", [OSError("no prctl"), AttributeError("no symbol")])
    def test_a_broken_libc_does_not_stop_the_process(self, monkeypatch, exc):
        """A consumer that refuses to start because it cannot harden itself protects nothing."""
        monkeypatch.setattr(process_hardening, "_load_libc", lambda: _FakeLibc(raises=exc))
        assert make_non_dumpable() is False

    def test_no_libc_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(process_hardening, "_load_libc", lambda: None)
        assert make_non_dumpable() is False


class TestLoadLibc:
    def test_non_linux_platforms_get_nothing(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert process_hardening._load_libc() is None

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux only")
    def test_linux_resolves_a_libc_with_prctl(self):
        libc = process_hardening._load_libc()
        assert libc is not None and hasattr(libc, "prctl")


class TestCliCallsIt:
    def test_the_cli_hardens_at_import_time_not_inside_main(self):
        """Placement is the point, twice over.

        Every cron-spawned `bin/agento` job carries the store env, not only the
        consumer — so this belongs at the CLI entry, not in `Consumer`. And it must run
        at IMPORT time: reaching `main()` costs ~200 ms of bootstrap, all of it with the
        process's environ readable by a same-uid peer.
        """
        import ast
        import pathlib

        import agento.framework.cli as cli

        tree = ast.parse(pathlib.Path(cli.__file__).read_text())
        module_level = [
            node.value.func.id
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ]
        assert "make_non_dumpable" in module_level
