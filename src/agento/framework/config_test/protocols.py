"""Config-field test contract — what a tester returns, and the two kinds.

A tester is a diagnostic, not an access gate: it answers "do these stored
credentials work right now?" without mutating anything. Its result carries a
machine-readable ``status`` (and an optional finer ``code``) plus a single-line
message safe to print.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

OK = "ok"
FAIL = "fail"
NOT_CONFIGURED = "not_configured"
ERROR = "error"

STATUSES = (OK, FAIL, NOT_CONFIGURED, ERROR)

# The framework has exactly two arms. Every declarative kind the toolbox
# understands (`smtp`, `http`, a named probe) is KIND_TOOLBOX from here: the
# framework does not interpret them, so it never grows a branch per kind.
KIND_TOOLBOX = "toolbox"
KIND_LOCAL = "local"
KINDS = (KIND_TOOLBOX, KIND_LOCAL)

# The kinds the TOOLBOX implements, and the key each one cannot run without.
# One file per kind lives in `src/agento/toolbox/probes/`, and a parity test
# fails if this table and that directory disagree — the table is duplicated
# across two languages, so it is guarded rather than trusted.
BUILTIN_TOOLBOX_KINDS = ("http", "smtp")
REQUIRED_SPEC_FIELDS = {"http": ("url",), "smtp": ("host",)}

# A code is a machine-readable label, never prose. Constraining its shape is
# what lets a result return it unsanitized: an upstream error string cannot
# be smuggled through it.
CODE_RE = re.compile(r"^[A-Z0-9_]{1,40}$")


@dataclass(frozen=True)
class TestResult:
    """Outcome of one config test.

    ``status`` is one of ``STATUSES``. ``code`` is an optional refinement (e.g.
    ``PAIR_MISMATCH``) for callers that need to branch on the reason.
    ``message`` is one line for a human and MUST NOT contain a secret.
    """

    # pytest collects any class named Test*; this is a result object, not a
    # test case, and without this every test module importing it warns.
    __test__ = False

    status: str
    message: str
    code: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass(frozen=True)
class TesterRef:
    """What a field's ``tester`` key resolved to.

    ``label`` is for display and for the CLI/TUI ("press 't' to run 'smtp'") —
    the named probe's name, or the declarative kind. ``class_path`` is set for
    ``KIND_LOCAL`` only.
    """

    kind: str
    label: str
    module: str
    module_dir: Path
    class_path: str = ""


class LocalTester(Protocol):
    def run(self, conn, *, scope: str, scope_id: int) -> TestResult:
        """Probe the live system, resolving whatever config this module already
        reads. Missing or empty values must yield ``NOT_CONFIGURED``; a failure
        to read a stored value must yield ``ERROR``, never ``NOT_CONFIGURED``."""
        ...


__all__ = [
    "BUILTIN_TOOLBOX_KINDS",
    "CODE_RE",
    "ERROR",
    "FAIL",
    "KINDS",
    "KIND_LOCAL",
    "KIND_TOOLBOX",
    "NOT_CONFIGURED",
    "OK",
    "REQUIRED_SPEC_FIELDS",
    "STATUSES",
    "LocalTester",
    "TestResult",
    "TesterRef",
]
