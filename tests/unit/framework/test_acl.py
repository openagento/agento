"""The ACL registry: ``acl.json`` files of the framework and the modules (D-ADMIN-ACL-2)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agento.framework.access import acl
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.module_validator import validate_all, validate_module
from tests.module_writer import write_module


def _screen(title: str, *ids: str) -> dict:
    return {"title": title, "leaves": [{"id": i, "title": "t"} for i in ids]}


def test_core_first_then_modules_by_name(tmp_path):
    b = write_module(tmp_path, "beta", acl_json={"screens": [_screen("Beta", "beta")],
                                   "operations": [{"id": "beta.run", "title": "Run"}]})
    a = write_module(tmp_path, "alpha", acl_json={"screens": [_screen("Alpha", "alpha", "alpha.edit")]})

    registry = acl.load([("beta", b), ("alpha", a)])

    core_titles = [t for t, _ in acl.core().screens]
    assert [t for t, _ in registry.screens] == [*core_titles, "Alpha", "Beta"]
    assert registry.leaf_owners["alpha.edit"] == "alpha"
    assert registry.leaf_owners["admin.jobs"] == ""
    assert registry.operations["beta.run"] == "Run"
    assert registry.core_operations == {"artifact.launch"}
    assert "beta.run" not in registry.resources  # an operation is not a panel leaf


@pytest.mark.parametrize(("data", "why"), [
    ([], "must be an object"),
    ({"screens": {}}, "'screens' must be a list"),
    ({"operations": "x"}, "'operations' must be a list"),
    ({"leaves": []}, "unknown key 'leaves'"),
    ({"screens": [{"title": "Demo", "leaves": []}]}, "at least one leaf"),
    ({"screens": [_screen("Demo", "other.view")]}, "'other.view' must match"),   # no module prefix
    ({"screens": [_screen("Demo", "demox")]}, "'demox' must match"),
    ({"screens": [_screen("Demo", "demo.View")]}, "'demo.View' must match"),
    ({"operations": [{"id": "demo.run"}]}, "non-empty title"),
    ({"operations": [{"id": "demo.run", "title": "x", "extra": 1}]}, "non-empty title"),
    ({"screens": [_screen("Demo", "demo")], "operations": [{"id": "demo", "title": "x"}]}, "declared twice"),
])
def test_a_bad_file_is_refused_by_validate_and_by_load(tmp_path, data, why):
    mod = write_module(tmp_path, "demo", acl_json=data)

    assert any(why in e for e in validate_module(mod)), validate_module(mod)
    with pytest.raises(acl.AclError) as exc:
        acl.load([("demo", mod)])
    assert why in str(exc.value)


def test_an_unparsable_file_is_refused(tmp_path):
    mod = write_module(tmp_path, "demo", acl_json="{")

    assert "acl.json: invalid JSON" in validate_module(mod)
    with pytest.raises(acl.AclError):
        acl.load([("demo", mod)])


def test_an_id_is_unique_across_core_and_modules(tmp_path):
    core, user = tmp_path / "core", tmp_path / "user"
    write_module(core, "alpha", acl_json={"screens": [_screen("A", "alpha")]})
    write_module(core, "admin", acl_json={"screens": [_screen("Jobs again", "admin.jobs")]})   # a core id
    write_module(user, "alpha_two", acl_json={"operations": [{"id": "alpha_two.run", "title": "x"}]})

    results = validate_all(core, user)

    assert any("'admin.jobs' is already declared by the framework" in e for e in results["admin"])
    assert "alpha" not in results and "alpha_two" not in results
    with pytest.raises(acl.AclError):
        acl.load([("alpha", core / "alpha"), ("admin", core / "admin")])


def test_a_leftover_di_acl_resources_key_is_a_validate_error(tmp_path):
    mod = write_module(tmp_path, "demo", di_json={"acl_resources": [{"id": "demo.run", "title": "Run"}]})

    assert any("'acl_resources' is not read" in e for e in validate_module(mod))


ROUTE = {"method": "GET", "path": "/api/demo/x", "handler": "src.routes.x"}


@pytest.mark.parametrize(("value", "ok"), [
    ("self", True), ("demo.view", True), ("admin.jobs", True), ("other", True),
    ("stranger", False), ("demo.nothing", False), ("anonymous", False), (None, False),
])
def test_validate_checks_a_route_acl_against_own_core_and_sequence_leaves(tmp_path, value, ok):
    core, user = tmp_path / "core", tmp_path / "user"
    write_module(core, "other", acl_json={"screens": [_screen("Other", "other")]})
    write_module(core, "stranger", acl_json={"screens": [_screen("Stranger", "stranger")]})
    route = ROUTE if value is None else {**ROUTE, "acl": value}
    write_module(core, "demo", module_json={"sequence": ["other"]},
                 acl_json={"screens": [_screen("Demo", "demo.view")]}, di_json={"routes": [route]})

    errors = validate_all(core, user).get("demo", [])

    assert (not [e for e in errors if "acl" in e]) is ok, errors


def test_the_shipped_files_load():
    from agento.framework.module_discovery import module_dirs_by_name

    registry = acl.load(module_dirs_by_name(CORE_MODULES_DIR, "/nonexistent"))

    assert {"versioned_artifacts", "versioned_artifacts.publish",
            "versioned_artifacts.delete"} <= registry.resources
    assert {"conversation.run_details", "conversation.channel_write", "artifact.launch"} <= set(registry.operations)


def test_no_module_handler_checks_a_panel_leaf():
    """Class guard: a module route names its leaf in di.json `acl`, and web checks it before the
    handler. A handler that calls `accounts.may` or `held_admin` is a second, forgettable gate."""
    found = []
    for path in Path(CORE_MODULES_DIR).glob("*/src/**/*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("may", "held_admin") \
                    and isinstance(node.value, ast.Name) and node.value.id == "accounts":
                found.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "agento.framework.access.accounts" \
                    and any(a.name in ("may", "held_admin") for a in node.names):
                found.append(f"{path}:{node.lineno}")
    assert found == []
