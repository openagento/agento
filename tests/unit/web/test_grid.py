from __future__ import annotations

import pytest

from agento.web import grid
from agento.web.api import Response

SORTS = ("id", "type")


def _parse(**query):
    return grid.parse_grid(query, sorts=SORTS, default_sort="id")


def test_defaults():
    assert _parse() == grid.GridQuery(page=1, page_size=grid.DEFAULT_PAGE_SIZE, q=None, sort="id", desc=True)


def test_values_are_read_and_q_is_trimmed():
    g = _parse(page="3", page_size="25", q="  AG-7 ", sort="type", dir="asc")
    assert g == grid.GridQuery(page=3, page_size=25, q="AG-7", sort="type", desc=False)
    assert g.offset == 50


def test_blank_q_is_none():
    assert _parse(q="   ").q is None


@pytest.mark.parametrize("query", [
    {"page": "0"}, {"page": "-1"}, {"page": "x"}, {"page": ""}, {"page": "1.5"}, {"page": "²"}, {"page": "٣"},
    {"page": str(grid.MAX_PAGE + 1)}, {"page": "9" * 30},
    {"page_size": "10"}, {"page_size": "abc"},
    {"q": "x" * (grid.MAX_Q + 1)},
    {"sort": "error_message"}, {"dir": "up"},
])
def test_bad_value_is_400(query):
    resp = _parse(**query)
    assert isinstance(resp, Response) and resp.status == 400


@pytest.mark.parametrize("q, pattern", [
    ("abc", "%abc%"),
    ("50%", "%50\\%%"),
    ("a_b", "%a\\_b%"),
    ("c:\\x", "%c:\\\\x%"),
])
def test_contains_escapes_like_wildcards(q, pattern):
    assert grid.contains(q) == pattern


def test_page_body():
    g = _parse(page="2")
    assert grid.page_body([{"id": 1}], 51, g) == {"rows": [{"id": 1}], "total": 51, "page": 2, "page_size": 50}
