"""The outlook Test action must not resolve a Graph credential in Python.

`modules/outlook/src/config.py` states the rule this test defends: the Graph
credentials are resolved by the TOOLBOX, and are deliberately not fields on the
Python dataclass so the framework never holds them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.config_test import KIND_TOOLBOX
from agento.framework.config_test import tester_for_field as _tester_for_field

MODULE_DIR = Path("src/agento/modules/outlook")
CRED_FIELDS = (
    "outlook_tenant_id", "outlook_client_id", "outlook_client_secret",
    "outlook_cert_pem", "outlook_cert_password", "outlook_mailbox_user_id",
)


@pytest.fixture(scope="module")
def system():
    return json.loads((MODULE_DIR / "system.json").read_text())


def test_every_credential_field_points_at_the_named_probe(system):
    for field in CRED_FIELDS:
        assert system[field].get("tester") == "graph_credentials", field


def test_the_named_form_resolves_to_the_toolbox_arm():
    ref = _tester_for_field("outlook/outlook_client_secret")
    assert ref is not None
    assert ref.kind == KIND_TOOLBOX
    assert ref.label == "graph_credentials"
    assert ref.class_path == ""


def test_the_probe_name_matches_the_javascript(system):
    js = (MODULE_DIR / "toolbox" / "credentials.js").read_text()
    assert "name: 'graph_credentials'" in js


def test_the_probe_declares_every_field_that_points_at_it(system):
    """A field pointing at a probe that never resolves it gets a button that
    tests something else. The two lists must agree."""
    js = (MODULE_DIR / "toolbox" / "credentials.js").read_text()
    for field in CRED_FIELDS:
        assert f"'outlook/{field}'" in js, field


def test_the_declaration_needs_no_di_json_entry():
    """The named form is declared by the field and the toolbox export. If a
    `config_testers` block ever appears in a di.json, the mechanism grew a
    second registration path — which is the thing this design removed."""
    di = json.loads((MODULE_DIR / "di.json").read_text())
    assert "config_testers" not in di


def test_the_healthcheck_is_exported_from_exactly_one_discovered_file():
    """`discoverToolboxFiles` imports every .js in toolbox/ and registers each
    one's `healthcheck` export, so a re-export means the probe runs twice and
    `/health`'s checks[] carries two `outlook` entries."""
    exporters = [
        f.name for f in (MODULE_DIR / "toolbox").glob("*.js")
        if "export async function healthcheck" in f.read_text()
        or "export { healthcheck" in f.read_text()
    ]
    assert exporters == ["credentials.js"]


def test_config_tests_is_exported_from_exactly_one_discovered_file():
    """A name registered twice fails CLOSED — both declarations are discarded
    and the field answers DUPLICATE_TESTER — so a duplicate export disables the
    probe entirely."""
    exporters = [
        f.name for f in (MODULE_DIR / "toolbox").glob("*.js")
        if "export const configTests" in f.read_text()
    ]
    assert exporters == ["credentials.js"]


def test_no_test_file_sits_in_the_toolbox_directory():
    """That glob does not exclude *.test.js — a test file there is imported in
    production. JS tests belong in src/agento/toolbox/tests/."""
    assert list((MODULE_DIR / "toolbox").glob("*.test.js")) == []


def test_the_polling_fields_have_no_tester(system):
    """Only the credential path gets the button."""
    for field, schema in system.items():
        if field not in CRED_FIELDS:
            assert "tester" not in schema, field
