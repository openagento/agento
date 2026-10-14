"""The ACL registry: ``acl.json`` files of the framework and the modules (D-ADMIN-ACL-2)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agento.framework.access import acl
from agento.framework.bootstrap import CORE_MODULES_DIR
from agento.framework.module_validator import validate_all, validate_module


def _module(root: Path, name: str, acl_json: object | None = None, *, sequence=(), routes=None) -> Path:
    mod = root / name
    mod.mkdir(parents=True)
    (mod / "module.json").write_text(json.dumps(
        {"name": name, "version": "1.0.0", "description": name, "sequence": list(sequence)}))
    if acl_json is not None:
        (mod / "acl.json").write_text(json.dumps(acl_json))
    if routes is not None:
        (mod / "di.json").write_text(json.dumps({"routes": routes}))
    return mod


def _screen(title: str, *ids: str) -> dict:
    return {"title": title, "leaves": [{"id": i, "title": "t"} for i in ids]}


def test_core_first_then_modules_by_name(tmp_path):
    b = _module(tmp_path, "beta", {"screens": [_screen("Beta", "beta")],
                                   "operations": [{"id": "beta.run", "title": "Run"}]})
    a = _module(tmp_path, "alpha", {"screens": [_screen("Alpha", "alpha", "alpha.edit")]})

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
    mod = _module(tmp_path, "demo", data)

    assert any(why in e for e in validate_module(mod)), validate_module(mod)
    with pytest.raises(acl.AclError) as exc:
        acl.load([("demo", mod)])
    assert why in str(exc.value)


def test_an_unparsable_file_is_refused(tmp_path):
    mod = _module(tmp_path, "demo")
    (mod / "acl.json").write_text("{")

    assert "acl.json: invalid JSON" in validate_module(mod)
    with pytest.raises(acl.AclError):
        acl.load([("demo", mod)])


def test_an_id_is_unique_across_core_and_modules(tmp_path):
    core, user = tmp_path / "core", tmp_path / "user"
    _module(core, "alpha", {"screens": [_screen("A", "alpha")]})
    _module(core, "admin", {"screens": [_screen("Jobs again", "admin.jobs")]})   # a core id
    _module(user, "alpha_two", {"operations": [{"id": "alpha_two.run", "title": "x"}]})

    results = validate_all(core, user)

    assert any("'admin.jobs' is already declared by the framework" in e for e in results["admin"])
    assert "alpha" not in results and "alpha_two" not in results
    with pytest.raises(acl.AclError):
        acl.load([("alpha", core / "alpha"), ("admin", core / "admin")])


def test_a_leftover_di_acl_resources_key_is_a_validate_error(tmp_path):
    mod = _module(tmp_path, "demo")
    (mod / "di.json").write_text(json.dumps({"acl_resources": [{"id": "demo.run", "title": "Run"}]}))

    assert any("'acl_resources' is not read" in e for e in validate_module(mod))


ROUTE = {"method": "GET", "path": "/api/demo/x", "handler": "src.routes.x"}


@pytest.mark.parametrize(("value", "ok"), [
    ("self", True), ("demo.view", True), ("admin.jobs", True), ("other", True),
    ("stranger", False), ("demo.nothing", False), ("anonymous", False), (None, False),
])
def test_validate_checks_a_route_acl_against_own_core_and_sequence_leaves(tmp_path, value, ok):
    core, user = tmp_path / "core", tmp_path / "user"
    _module(core, "other", {"screens": [_screen("Other", "other")]})
    _module(core, "stranger", {"screens": [_screen("Stranger", "stranger")]})
    route = ROUTE if value is None else {**ROUTE, "acl": value}
    _module(core, "demo", {"screens": [_screen("Demo", "demo.view")]}, sequence=["other"], routes=[route])

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
