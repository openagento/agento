"""The app_monitor alert credentials must be testable from the admin TUI.

This is the credential whose silent 535 started the whole feature: the alert
fired, `smtp.login()` was rejected, the observer swallowed the exception, and the
warning never reached logs/consumer.log. A button that reproduces the 535 on
demand is the fix for the diagnosis problem; Task 12 fixes the silence.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.config_test.manifest import KIND_TOOLBOX, field_schemas

MODULE_DIR = Path("src/agento/modules/app_monitor")
FIELDS = (
    "alerts/smtp_host", "alerts/smtp_port", "alerts/smtp_user",
    "alerts/smtp_password", "alerts/smtp_tls",
)


@pytest.fixture(scope="module")
def system():
    return json.loads((MODULE_DIR / "system.json").read_text())


def test_every_smtp_field_carries_the_declaration(system):
    """Any of them being wrong stops the alert, so any of them gets the button."""
    for field in FIELDS:
        assert system[field].get("tester", {}).get("kind") == "smtp", field


def test_the_declaration_references_this_module_only(system):
    spec = system["alerts/smtp_password"]["tester"]
    assert spec["host"] == "{app_monitor/alerts/smtp_host}"
    assert spec["port"] == "{app_monitor/alerts/smtp_port}"
    assert spec["user"] == "{app_monitor/alerts/smtp_user}"
    assert spec["pass"] == "{app_monitor/alerts/smtp_password}"
    assert spec["starttls"] == "{app_monitor/alerts/smtp_tls}"


def test_every_referenced_field_exists(system):
    """A placeholder naming an absent field resolves to None for ever — a test
    button that can only ever say "not configured". `module:validate` reports it;
    this asserts the shipped manifest is clean."""
    declared = set(system)
    for field in FIELDS:
        spec = system[field]["tester"]
        for value in spec.values():
            if isinstance(value, str) and value.startswith("{app_monitor/"):
                assert value[len("{app_monitor/"):-1] in declared, value


def test_the_password_field_is_still_obscure(system):
    """The declaration must not have been pasted over the type."""
    assert system["alerts/smtp_password"]["type"] == "obscure"


def test_the_tester_resolves_as_a_toolbox_arm():
    """`smtp` is interpreted by the toolbox, so the framework must classify it as
    the toolbox arm and resolve nothing itself."""
    from agento.framework.config_test import tester_for_field as _tester_for_field

    ref = _tester_for_field("app_monitor/alerts/smtp_password")
    assert ref is not None
    assert ref.kind == KIND_TOOLBOX
    assert ref.label == "smtp"
    assert ref.class_path == ""


def test_no_python_probe_was_added():
    """The probe lives in the toolbox, where the credential already is. A Python
    smtplib probe here would put the framework back in the business of
    decrypting a secret in order to test it."""
    assert not Path("src/agento/framework/probes").exists()


def test_the_module_still_needs_no_toolbox_directory(tmp_path):
    """A declarative kind is the whole point: app_monitor ships no JavaScript."""
    assert not (MODULE_DIR / "toolbox").exists()
    assert field_schemas(MODULE_DIR) is not None
