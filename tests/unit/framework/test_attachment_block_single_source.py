"""The block that names a job's files has ONE producer: `artifacts_dir.attachment_block`,
appended by `Workflow.execute` to every run. A module that builds the block itself can forget
it (it did, AG-87) or drift from the delivered paths (CLS-1)."""
from __future__ import annotations

import ast
from pathlib import Path

import agento

_MARKER = "Attached files:"


def _producers() -> set[tuple[str, str]]:
    """`(file, innermost function)` of every string constant in `src/agento` holding the marker."""
    root = Path(agento.__file__).parent
    found = set()

    def visit(node: ast.AST, path: str, owner: str) -> None:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return                                  # a docstring describes; it builds nothing
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _MARKER in node.value:
            found.add((path, owner))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = node.name
        for child in ast.iter_child_nodes(node):
            visit(child, path, owner)

    for path in root.rglob("*.py"):
        visit(ast.parse(path.read_text(encoding="utf-8")), path.relative_to(root).as_posix(), "<module>")
    return found


def test_only_the_framework_builds_the_attachment_block():
    assert _producers() == {("framework/artifacts_dir.py", "attachment_block")}
