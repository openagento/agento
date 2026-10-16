"""Tests for .env file parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from agento.framework.cli._env import parse_env_file


class TestParseEnvFile:
    @pytest.mark.parametrize(("text", "expected"), [
        pytest.param("KEY=value\n", {"KEY": "value"}, id="simple_key_value"),
        pytest.param("# comment\nKEY=value\n", {"KEY": "value"}, id="skips_comments"),
        pytest.param("\nKEY=value\n\n", {"KEY": "value"}, id="skips_empty_lines"),
        pytest.param('KEY="quoted value"\n', {"KEY": "quoted value"}, id="strips_double_quotes"),
        pytest.param("KEY='quoted value'\n", {"KEY": "quoted value"}, id="strips_single_quotes"),
        pytest.param("KEY=\n", {"KEY": ""}, id="empty_value"),
        pytest.param("KEY=a=b=c\n", {"KEY": "a=b=c"}, id="value_with_equals"),
        pytest.param("A=1\nB=2\nC=3\n", {"A": "1", "B": "2", "C": "3"}, id="multiple_entries"),
        pytest.param("INVALID\nKEY=value\n", {"KEY": "value"}, id="skips_lines_without_equals"),
    ])
    def test_parses(self, tmp_path: Path, text, expected):
        env_file = tmp_path / ".env"
        env_file.write_text(text)
        assert parse_env_file(env_file) == expected

    def test_nonexistent_file(self, tmp_path: Path):
        assert parse_env_file(tmp_path / "missing") == {}
