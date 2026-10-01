"""The framework writes no module table (§6.4.1, PLC-2).

A plain word search would fire on the word "message" in a docstring and get weakened until
it proved nothing. This asserts the SHAPE instead: no SQL statement anywhere under
`src/agento/framework/` names a table the conversation module owns.
"""
from __future__ import annotations

import re
from pathlib import Path

FRAMEWORK = Path(__file__).resolve().parents[3] / "src" / "agento" / "framework"

# Every table `modules/conversation/sql/001_conversation.sql` creates.
MODULE_TABLES = {
    "conversation", "message", "execution", "conversation_event",
    "execution_delta", "conversation_prune_watermark",
}

# `FROM x`, `JOIN x`, `INTO x`, `UPDATE x` — with or without a backtick.
_SQL_TABLE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+`?([a-z_][a-z0-9_]*)`?", re.IGNORECASE)


def test_no_framework_sql_names_a_conversation_table():
    offenders: list[str] = []
    for path in sorted(FRAMEWORK.rglob("*.py")):
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            for table in _SQL_TABLE.findall(line):
                if table.lower() in MODULE_TABLES:
                    offenders.append(f"{path.relative_to(FRAMEWORK)}:{line_no}: {table}")

    assert offenders == [], (
        "the framework must reach a module's rows through an execution hook, never by "
        "naming its table:\n" + "\n".join(offenders)
    )


def test_the_guard_would_catch_a_violation():
    """The guard's own test: a regex that matched nothing would pass the test above."""
    assert _SQL_TABLE.findall("SELECT 1 FROM execution_delta WHERE x = 1") == ["execution_delta"]
    assert _SQL_TABLE.findall("INSERT INTO `message` (a) VALUES (1)") == ["message"]
    assert _SQL_TABLE.findall("UPDATE conversation SET state = 'archived'") == ["conversation"]
