"""CLS-1 guard (AG-67 D-28): the channel, not the owner, says whether a conversation is a
channel thread. A channel conversation may have an owner (a schedule's), so no code of the
module may read `user_id` NULL as "channel thread": it tests `channel != PANEL_CHANNEL`."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[4] / "src/agento/modules/conversation/src"
_SQL = re.compile(r"\buser_id\s+IS\s+(NOT\s+)?NULL", re.IGNORECASE)


def _names_user_id(node: ast.expr) -> bool:
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "user_id"
    if isinstance(node, ast.Attribute):
        return node.attr == "user_id"
    return isinstance(node, ast.Name) and node.id == "user_id"


def _offences(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_names_user_id(s) for s in sides) and any(
                    isinstance(s, ast.Constant) and s.value is None for s in sides):
                lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and _SQL.search(node.value):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_no_code_reads_a_null_owner_as_a_channel_thread(path):
    assert _offences(ast.parse(path.read_text())) == []


@pytest.mark.parametrize("source", [
    'row["user_id"] is None', "row.user_id is not None", "user_id == None",
    'cur.execute("SELECT 1 WHERE c.user_id IS NULL")'])
def test_the_guard_sees_each_shape(source):
    assert _offences(ast.parse(source)) != []


def test_the_guard_ignores_the_author_column():
    assert _offences(ast.parse('q = "WHERE m.author_user_id IS NULL"')) == []
