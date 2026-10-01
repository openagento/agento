import json
from pathlib import Path

import pytest

from agento.web.app_path import parse_app_path

FIXTURE = json.loads((Path(__file__).parents[2] / "fixtures" / "app_path_v1.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["accept"], ids=lambda c: c["raw"])
def test_accepts_and_canonicalizes(case):
    parsed = parse_app_path(case["raw"])
    assert parsed is not None
    assert parsed.upstream_path == case["upstream"]
    code, _, version = case["upstream"].split("/")[1:4]
    assert (parsed.artifact_code, parsed.version_id) == (code, version)


@pytest.mark.parametrize("raw", FIXTURE["reject"])
def test_rejects(raw):
    assert parse_app_path(raw) is None


def test_upstream_path_is_a_fixed_point():
    # What web returns must parse to itself: one canonical spelling per file.
    for case in FIXTURE["accept"]:
        again = parse_app_path("/a" + case["upstream"])
        assert again is not None and again.upstream_path == case["upstream"]


def test_non_string_is_refused():
    assert parse_app_path(None) is None
