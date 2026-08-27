"""core's SMTP credential gets the same button — and no second probe."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

MODULE_DIR = Path("src/agento/modules/core")
FIELDS = ("smtp_host", "smtp_port", "smtp_user", "smtp_pass")


@pytest.fixture(scope="module")
def system():
    return json.loads((MODULE_DIR / "system.json").read_text())


def test_every_smtp_field_carries_the_declaration(system):
    for field in FIELDS:
        assert system[field].get("tester", {}).get("kind") == "smtp", field


def test_the_declaration_references_core_fields(system):
    spec = system["smtp_pass"]["tester"]
    assert spec == {
        "kind": "smtp",
        "host": "{core/smtp_host}",
        "port": "{core/smtp_port}",
        "user": "{core/smtp_user}",
        "pass": "{core/smtp_pass}",
    }


def test_smtp_from_has_no_tester(system):
    """The from-address is not a credential — nothing to probe."""
    assert "tester" not in system["smtp_from"]


def test_the_existing_healthcheck_is_untouched():
    """`email_send`'s healthcheck stays the container healthcheck. The Test
    button is a per-field question and does not replace it."""
    js = (MODULE_DIR / "toolbox" / "email.js").read_text()
    assert "export async function healthcheck" in js
    assert "transporter.verify()" in js
