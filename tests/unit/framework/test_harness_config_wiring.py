"""Every command-building HarnessRunContext must carry harness_config.

An empty harness_config silently reverts a harness's own build-time settings — for
codex that is the sandbox bypass flag, i.e. an operator's network block that does not
hold. The allow-list below is the reviewed set of sites that legitimately build a
context without one; adding to it is a decision, not a formality.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "agento"

# path-relative allow-list — "<relative path>": "<reason>"
ALLOWED_WITHOUT: dict[str, str] = {}


def _contexts_without_harness_config() -> list[str]:
    offenders = []
    for path in SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "HarnessRunContext":
                continue
            if any(kw.arg == "harness_config" for kw in node.keywords):
                continue
            if any(kw.arg is None for kw in node.keywords):  # **kwargs passthrough
                continue
            offenders.append(str(path.relative_to(SRC)))
    return offenders


def test_every_harness_run_context_carries_harness_config():
    offenders = [p for p in _contexts_without_harness_config() if p not in ALLOWED_WITHOUT]
    assert offenders == [], (
        "HarnessRunContext built without harness_config — the harness's own settings "
        f"(codex sandbox_mode, pi builtin_tools) are silently lost on these paths: {offenders}"
    )
