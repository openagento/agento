"""Class guard: every write of `job.finished_at` keeps microseconds.

`threads.resolve_resume` orders the turns of a line by `finished_at`. Two turns of one
line can finish within one second in either id order, so a write at seconds precision
(`NOW()`) makes a tie that the id breaks the wrong way. The column is TIMESTAMP(6); this
test fails on any source statement that sets it from a clock with less precision.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src" / "agento"
SQL = SRC / "framework" / "sql"

# One UPDATE of the job table, up to its WHERE (the SET list).
_UPDATE_JOB = re.compile(r"UPDATE\s+job\s+SET\b(.*?)\bWHERE\b", re.S | re.I)
_FINISHED = re.compile(r"\bfinished_at\s*=\s*([^,\n]+)", re.I)


def _sources():
    for path in SRC.rglob("*"):
        if path.suffix in {".py", ".js"} and not {"node_modules", "assets"} & set(path.parts):
            yield path


def test_every_job_finished_at_write_uses_now6():
    found, bad = 0, []
    for path in _sources():
        for statement in _UPDATE_JOB.findall(path.read_text(errors="ignore")):
            for value in _FINISHED.findall(statement):
                found += 1
                if value.strip() != "NOW(6)":
                    bad.append(f"{path.relative_to(SRC)}: finished_at = {value.strip()}")
    assert found >= 3, "the scan found no terminal write; the pattern is stale"
    assert bad == []


def test_job_finished_at_column_keeps_microseconds():
    init = re.sub(r"[ \t]+", " ", (SQL / "init" / "000_init.sql").read_text())
    assert "finished_at TIMESTAMP(6) NULL," in init
    assert "MODIFY COLUMN finished_at TIMESTAMP(6) NULL" in (SQL / "054_thread.sql").read_text()
