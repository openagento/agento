"""Tests for module scaffolding."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.framework.module_scaffold import TOOL_FIELD_TEMPLATES, scaffold_module
from agento.framework.module_validator import validate_module


class TestScaffoldModule:
    def test_creates_directory_structure(self, tmp_path: Path):
        module_dir = scaffold_module("test-mod", tmp_path)

        assert module_dir.is_dir()
        assert (module_dir / "module.json").is_file()
        assert (module_dir / "config.json").is_file()
        assert (module_dir / "di.json").is_file()
        assert (module_dir / "events.json").is_file()
        assert (module_dir / "data_patch.json").is_file()
        assert (module_dir / "cron.json").is_file()
        assert (module_dir / "knowledge" / "README.md").is_file()
        assert (module_dir / "src" / "__init__.py").is_file()

    def test_generates_valid_module_json(self, tmp_path: Path):
        module_dir = scaffold_module("my-app", tmp_path, description="My App")

        manifest = json.loads((module_dir / "module.json").read_text())
        assert manifest["name"] == "my-app"
        assert manifest["version"] == "1.0.0"
        assert manifest["description"] == "My App"
        assert manifest["tools"] == []
        assert manifest["log_servers"] == []

    def test_with_tools(self, tmp_path: Path):
        module_dir = scaffold_module(
            "db-app", tmp_path,
            tools=["mysql:mysql_prod:Production DB"],
        )

        manifest = json.loads((module_dir / "module.json").read_text())
        assert len(manifest["tools"]) == 1
        tool = manifest["tools"][0]
        assert tool["type"] == "mysql"
        assert tool["name"] == "mysql_prod"
        assert tool["description"] == "Production DB"
        assert "host" in tool["fields"]
        assert tool["fields"]["pass"]["type"] == "obscure"

        config = json.loads((module_dir / "config.json").read_text())
        assert "tools" in config
        assert "mysql_prod" in config["tools"]

    def test_mysql_root_tool_declares_the_same_fields_as_mysql(self, tmp_path: Path):
        module_dir = scaffold_module(
            "sandbox-app", tmp_path,
            tools=["mysql_root:mysql_sandbox_root:Agent sandbox DB (full access)"],
        )

        manifest = json.loads((module_dir / "module.json").read_text())
        tool = manifest["tools"][0]
        assert tool["type"] == "mysql_root"
        assert tool["name"] == "mysql_sandbox_root"
        assert tool["fields"] == TOOL_FIELD_TEMPLATES["mysql"]

        config = json.loads((module_dir / "config.json").read_text())
        assert set(config["tools"]["mysql_sandbox_root"]) == set(TOOL_FIELD_TEMPLATES["mysql"])

    def test_rejects_a_read_only_tool_squatting_a_reserved_root_name(self, tmp_path: Path):
        with pytest.raises(ValueError, match="reserved"):
            scaffold_module(
                "squatter", tmp_path,
                tools=["mysql:customer_db_root:Read-only tool with a reserved name"],
            )

        assert not (tmp_path / "squatter").exists()

    def test_rejects_mysql_root_tool_without_the_name_marker(self, tmp_path: Path):
        """module:add must not be able to create a module that module:validate rejects."""
        with pytest.raises(ValueError, match="_root"):
            scaffold_module(
                "sandbox-app", tmp_path,
                tools=["mysql_root:mysql_sandbox:Agent sandbox DB"],
            )

        assert not (tmp_path / "sandbox-app").exists()

    def test_scaffolded_tools_declare_toolset_defaulting_to_module_name(self, tmp_path: Path):
        module_dir = scaffold_module(
            "db-app", tmp_path,
            tools=["mysql:mysql_prod:Production DB", "mysql_root:mysql_sandbox_root:Sandbox DB"],
        )

        manifest = json.loads((module_dir / "module.json").read_text())
        assert [t["toolset"] for t in manifest["tools"]] == ["db-app", "db-app"]

    def test_scaffolded_module_passes_module_validate(self, tmp_path: Path):
        """The documented `module:add --tool ...` output must satisfy module:validate,
        which setup:upgrade runs before applying anything."""
        module_dir = scaffold_module(
            "db-app", tmp_path,
            tools=[
                "mysql:mysql_prod:Production DB",
                "mysql_root:mysql_sandbox_root:Sandbox DB",
                "mssql:mssql_bi:BI warehouse",
                "opensearch:os_search:Product index",
            ],
        )

        assert validate_module(module_dir) == []

    def test_scaffolded_module_without_tools_passes_module_validate(self, tmp_path: Path):
        assert validate_module(scaffold_module("bare-app", tmp_path)) == []

    def test_rejects_invalid_name(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Invalid module name"):
            scaffold_module("MyApp", tmp_path)
        with pytest.raises(ValueError, match="Invalid module name"):
            scaffold_module("_private", tmp_path)
        with pytest.raises(ValueError, match="Invalid module name"):
            scaffold_module("has spaces", tmp_path)

    def test_rejects_existing_directory(self, tmp_path: Path):
        (tmp_path / "existing").mkdir()
        with pytest.raises(ValueError, match="already exists"):
            scaffold_module("existing", tmp_path)

    def test_companion_files_valid_json(self, tmp_path: Path):
        module_dir = scaffold_module("check-json", tmp_path)

        for filename in ("di.json", "events.json", "data_patch.json", "cron.json"):
            data = json.loads((module_dir / filename).read_text())
            assert isinstance(data, dict)
