"""Architecture boundary tests — ensure module isolation and framework independence."""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULES_DIR = ROOT / "src" / "agento" / "modules"
FRAMEWORK_DIR = ROOT / "src" / "agento" / "framework"
EXAMPLE_DIR = ROOT / "app" / "code" / "_example"


def _get_imports(filepath: Path) -> list[str]:
    """Extract all import module names from a Python file."""
    try:
        tree = ast.parse(filepath.read_text())
    except SyntaxError:
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def _get_module_dirs() -> list[Path]:
    """Get all core module directories."""
    return [
        d for d in sorted(MODULES_DIR.iterdir())
        if d.is_dir() and not d.name.startswith("_") and not d.name.startswith(".")
    ]


def _get_declared_dependencies(module_dir: Path) -> set[str]:
    """Read module.json and return the set of declared sequence dependencies."""
    manifest = module_dir / "module.json"
    if not manifest.is_file():
        return set()
    data = json.loads(manifest.read_text())
    return set(data.get("sequence", []))


class TestModuleIsolation:
    def test_no_cross_module_imports(self):
        """No module should import from another module (unless declared in sequence)."""
        violations = []
        for module_dir in _get_module_dirs():
            module_name = module_dir.name
            allowed = _get_declared_dependencies(module_dir)
            for py_file in module_dir.rglob("*.py"):
                for imp in _get_imports(py_file):
                    if imp.startswith("agento.modules."):
                        imported_module = imp.split(".")[2]
                        if imported_module != module_name and imported_module not in allowed:
                            violations.append(
                                f"{py_file.relative_to(ROOT)}: imports agento.modules.{imported_module}"
                            )
        assert not violations, "Cross-module imports found:\n" + "\n".join(violations)

    def test_no_pydantic_import(self):
        """Dataclasses only (DECISIONS.md 2026-10-08, 2026-10-09): pydantic is in the image
        only because FastAPI needs it, and our code never imports it (TST-2)."""
        violations = [
            str(py_file.relative_to(ROOT))
            for py_file in (ROOT / "src" / "agento").rglob("*.py")
            if any(imp.split(".")[0] in ("pydantic", "pydantic_core") for imp in _get_imports(py_file))
        ]
        assert not violations, violations

    def test_framework_does_not_import_modules(self):
        """Framework code should not import from agento.modules."""
        violations = []
        for py_file in FRAMEWORK_DIR.rglob("*.py"):
            for imp in _get_imports(py_file):
                if imp.startswith("agento.modules"):
                    violations.append(f"{py_file.relative_to(ROOT)}: imports {imp}")
        assert not violations, "Framework imports modules:\n" + "\n".join(violations)


class TestModuleManifests:
    def test_all_core_module_manifests_valid(self):
        """All core modules must pass validation."""
        from agento.framework.module_validator import validate_module

        for module_dir in _get_module_dirs():
            errors = validate_module(module_dir)
            assert not errors, f"Module '{module_dir.name}' has errors:\n" + "\n".join(errors)

    def test_example_module_valid(self):
        """The _example module must pass validation."""
        from agento.framework.module_validator import validate_module

        errors = validate_module(EXAMPLE_DIR)
        assert not errors, "Example module has errors:\n" + "\n".join(errors)


class TestDiJsonReferences:
    def test_di_json_class_references_resolve(self):
        """All class paths in di.json must resolve to existing .py files."""
        violations = []
        all_dirs = [*_get_module_dirs(), EXAMPLE_DIR]

        for module_dir in all_dirs:
            di_path = module_dir / "di.json"
            if not di_path.is_file():
                continue
            di = json.loads(di_path.read_text())
            for section in ("channels", "workflows", "commands"):
                for entry in di.get(section, []):
                    if isinstance(entry, dict) and "class" in entry:
                        class_path = entry["class"]
                        parts = class_path.rsplit(".", 1)
                        if len(parts) >= 2:
                            file_path = module_dir / (parts[0].replace(".", "/") + ".py")
                            if not file_path.is_file():
                                violations.append(
                                    f"{module_dir.name}/di.json: {section} class '{class_path}' "
                                    f"-> {file_path.relative_to(ROOT)} not found"
                                )

        assert not violations, "Unresolved di.json references:\n" + "\n".join(violations)

    def test_events_json_observer_references_resolve(self):
        """All observer class paths in events.json must resolve."""
        violations = []
        all_dirs = [*_get_module_dirs(), EXAMPLE_DIR]

        for module_dir in all_dirs:
            events_path = module_dir / "events.json"
            if not events_path.is_file():
                continue
            events = json.loads(events_path.read_text())
            # events.json format: {event_name: [observer_dicts]}
            for _event_name, observer_list in events.items():
                if not isinstance(observer_list, list):
                    continue
                for observer in observer_list:
                    if isinstance(observer, dict) and "class" in observer:
                        class_path = observer["class"]
                        parts = class_path.rsplit(".", 1)
                        if len(parts) >= 2:
                            file_path = module_dir / (parts[0].replace(".", "/") + ".py")
                            if not file_path.is_file():
                                violations.append(
                                    f"{module_dir.name}/events.json: observer '{class_path}' "
                                    f"-> {file_path.relative_to(ROOT)} not found"
                                )

        assert not violations, "Unresolved events.json references:\n" + "\n".join(violations)
