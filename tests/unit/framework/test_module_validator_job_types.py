"""module:validate rule 1 — job-type declarations (PRD E3-E5 §4.2)."""

from pathlib import Path

import pytest

from agento.framework.module_validator import (
    declaration_shape_errors,
    job_type_declarations,
    validate_job_type_grammar,
    validate_job_types,
    validate_module,
)
from tests.module_writer import write_module


def test_declaring_a_builtin_is_rejected(tmp_path: Path):
    mod = write_module(tmp_path, "bad", di_json={"job_types": ["blank"]})
    errors = validate_module(mod)
    assert any("built-in" in e and "blank" in e for e in errors), errors


def test_grammar_is_enforced(tmp_path: Path):
    mod = write_module(tmp_path, "bad2", di_json={"job_types": ["Conversation", "conv-1", "a" * 33]})
    errors = validate_module(mod)
    for offender in ("Conversation", "conv-1", "a" * 33):
        assert any(offender in e for e in errors), (offender, errors)


def test_a_valid_declaration_passes(tmp_path: Path):
    mod = write_module(tmp_path, "conversation", di_json={"job_types": ["conversation"]})
    assert validate_job_type_grammar(job_type_declarations(mod, {})) == []


def test_two_modules_claiming_one_value_collide():
    results = validate_job_types([("conversation", ["conversation"]), ("other", ["conversation"])])
    assert "other" in results
    assert "conversation" in results["other"][0]
    assert "conversation" not in results          # the first claimant is not at fault


def test_one_module_repeating_its_own_value_is_not_a_collision():
    assert validate_job_types([("conversation", ["conversation", "conversation"])]) == {}


def test_a_workflow_for_a_builtin_declares_no_job_type(tmp_path: Path):
    """Replacing a built-in's workflow is shipped behaviour (modules/jira_periodic_tasks)."""
    mod = write_module(tmp_path, "periodic", di_json={
        "workflows": [{"type": "cron", "class": "src.wf.MyCron"}],
    })
    assert job_type_declarations(mod, {}) == []
    assert not [e for e in validate_module(mod) if "job type" in e]


# --- module:validate rule 2 — route declarations (PRD E3-E5 §11) --------------------


TWICE = {"method": "GET", "path": "/api/twice/x", "handler": "src.routes.hello", "acl": "self"}


@pytest.mark.parametrize(("name", "routes", "expected"), [
    pytest.param("demo", [{"method": "GET", "path": "/api/session", "handler": "src.routes.hello"}],
                 "/api/demo/", id="outside-the-module-prefix"),
    pytest.param("demo2", [{"method": "GET", "path": "/api/demo2/x", "handler": "src.routes.hello",
                            "auth": "login"}],
                 "auth", id="unauthenticated"),
    pytest.param("twice", [TWICE, TWICE], "declared twice", id="one-route-twice"),
    # There is no cross-module collision rule because the prefix makes one impossible.
    pytest.param("other", [{"method": "GET", "path": "/api/demo/x", "handler": "src.routes.hello"}],
                 "/api/other/", id="another-modules-path"),
])
def test_a_bad_route_declaration_is_rejected(tmp_path: Path, name, routes, expected):
    mod = write_module(tmp_path, name, di_json={"routes": routes})
    errors = validate_module(mod)
    assert any(expected in e for e in errors), errors


def test_a_valid_route_declaration_passes(tmp_path: Path):
    from agento.framework.module_validator import route_declarations, validate_route_grammar

    mod = write_module(tmp_path, "demo3", di_json={"routes": [
        {"method": "POST", "path": "/api/demo3/messages", "handler": "src.routes.send", "acl": "self"},
    ]})
    assert validate_route_grammar("demo3", route_declarations(mod, {}), frozenset()) == []


@pytest.mark.parametrize(("di", "expected"), [
    ({"job_types": "conversation"}, "'job_types' must be a list"),
    ({"job_types": ["ok", 5]}, "is not a non-empty string"),
    ({"job_types": ["ok", ""]}, "is not a non-empty string"),
    ({"routes": {}}, "'routes' must be a list"),
    ({"routes": "/api/x"}, "'routes' must be a list"),
    ({"execution_hooks": []}, "'execution_hooks' must be an object"),
    ({"execution_hooks": {"sink": 5}}, "must map a string seam"),
    # Moved to acl.json "operations" (D-ADMIN-ACL-2): a leftover key is an error, never ignored.
    ({"acl_resources": [{"id": "shapes.run", "title": "x"}]}, "'acl_resources' is not read"),
])
def test_a_malformed_declaration_is_refused_not_ignored(tmp_path: Path, di, expected):
    """PLN-3. The three readers mirror the LOADER, which is lenient so a bad manifest cannot
    crash bootstrap. Validation inherited that leniency and normalized every malformed
    declaration into an absent one, so the pre-install gate passed manifests that then failed
    or silently did nothing at startup. Wrong container type and wrong element type, for all
    three keys - one test for the class."""
    mod = write_module(tmp_path, "shapes", di_json=di)
    errors = declaration_shape_errors(mod, {})
    assert any(expected in e for e in errors), (expected, errors)
    assert any(expected in e for e in validate_module(mod)), "module:validate must surface it"


@pytest.mark.parametrize("di", [{}, {"job_types": []}, {"routes": []}, {"execution_hooks": {}}])
def test_an_absent_or_empty_declaration_is_not_an_error(tmp_path: Path, di):
    """Present means checked; absent means absent. A key nobody wrote is not a violation."""
    assert declaration_shape_errors(write_module(tmp_path, "fine", di_json=di), {}) == []
