"""MySQL is sized for 200 parallel jobs in both compose files (RULES.md SCL-1)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agento.framework.cli._provisioning import render_compose

ROOT = Path(__file__).parents[4]
TEMPLATE = ROOT / "src" / "agento" / "framework" / "cli" / "templates" / "docker-compose.yml"
DEV = ROOT / "docker" / "docker-compose.dev.yml"


@pytest.mark.parametrize("text", [
    render_compose(TEMPLATE.read_text(), python_version="3.12", extensions=[], sandbox_packages=[]),
    DEV.read_text(),
], ids=["template", "dev"])
def test_mysql_allows_600_connections(text):
    mysql = re.search(r"^  mysql:\n(?:(?:    .*)?\n)+", text, re.M).group(0)
    assert "command: --max-connections=600\n" in mysql
