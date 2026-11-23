"""One page of a server-side grid: the query parameters a panel grid sends, checked once.

A route lists the sort keys it accepts and maps each one to SQL itself; this module never builds SQL.
Query string: ``page`` (1-based), ``page_size``, ``q`` (search), ``sort``, ``dir`` (``asc``|``desc``).
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .api import Response, error

PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 50
MAX_Q = 200
MAX_PAGE = 100_000
_DIGITS = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True)
class GridQuery:
    page: int
    page_size: int
    q: str | None
    sort: str
    desc: bool

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def _int(value: str) -> int | None:
    return int(value) if _DIGITS.fullmatch(value) else None


def parse_grid(query: Mapping[str, str], *, sorts: Collection[str], default_sort: str,
               default_desc: bool = True) -> GridQuery | Response:
    page = _int(query.get("page", "1"))
    if page is None or not 1 <= page <= MAX_PAGE:
        return error(400, f"page must be an integer from 1 to {MAX_PAGE}")
    page_size = _int(query.get("page_size", str(DEFAULT_PAGE_SIZE)))
    if page_size not in PAGE_SIZES:
        return error(400, f"page_size must be one of {', '.join(map(str, PAGE_SIZES))}")
    q = query.get("q", "").strip()
    if len(q) > MAX_Q:
        return error(400, f"q must be at most {MAX_Q} characters")
    sort = query.get("sort", default_sort)
    if sort not in sorts:
        return error(400, f"sort must be one of {', '.join(sorts)}")
    direction = query.get("dir")
    if direction not in (None, "asc", "desc"):
        return error(400, "dir must be asc or desc")
    desc = default_desc if direction is None else direction == "desc"
    return GridQuery(page=page, page_size=page_size, q=q or None, sort=sort, desc=desc)


def contains(q: str) -> str:
    """A LIKE pattern that matches ``q`` literally anywhere; use it with ``ESCAPE '\\\\'``."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def page_body(rows: list, total: int, g: GridQuery) -> dict:
    return {"rows": rows, "total": total, "page": g.page, "page_size": g.page_size}
