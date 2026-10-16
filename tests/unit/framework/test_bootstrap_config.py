"""Tests for bootstrap config resolution — get_module_config()."""
import json
import os
from pathlib import Path

from agento.framework.bootstrap import bootstrap, get_module_config
from agento.framework.router_registry import get_routers


def _make_module(base_dir: Path, name: str, *, config: dict | None = None,
                 config_defaults: dict | None = None, order: int = 1000) -> None:
    """Create a minimal module directory with module.json, system.json, and optional config.json."""
    mod_dir = base_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": "1.0.0", "description": "", "order": order}
    (mod_dir / "module.json").write_text(json.dumps(manifest))
    if config:
        (mod_dir / "system.json").write_text(json.dumps(config))
    if config_defaults:
        (mod_dir / "config.json").write_text(json.dumps(config_defaults))


class TestGetModuleConfig:
    def test_module_with_config_defaults(self, tmp_path):
        _make_module(
            tmp_path, "mymod",
            config={
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            config_defaults={"host": "localhost", "port": 8080},
        )
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        cfg = get_module_config("mymod")
        assert cfg["host"] == "localhost"
        assert cfg["port"] == 8080

    def test_config_json_provides_defaults(self, tmp_path):
        _make_module(
            tmp_path, "mymod",
            config={"host": {"type": "string"}},
            config_defaults={"host": "from-config-json"},
        )
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        cfg = get_module_config("mymod")
        assert cfg["host"] == "from-config-json"

    def test_env_overrides_config_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG__MYMOD__HOST", "from-env")
        _make_module(
            tmp_path, "mymod",
            config={"host": {"type": "string"}},
            config_defaults={"host": "from-config-json"},
        )
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        cfg = get_module_config("mymod")
        assert cfg["host"] == "from-env"

    def test_module_without_config_returns_empty(self, tmp_path):
        _make_module(tmp_path, "noconfig")
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        assert get_module_config("noconfig") == {}

    def test_unknown_module_returns_empty(self, tmp_path):
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        assert get_module_config("nonexistent") == {}

    def test_re_bootstrap_clears_old_configs(self, tmp_path):
        _make_module(
            tmp_path, "mymod",
            config={"host": {"type": "string"}},
            config_defaults={"host": "v1"},
        )
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        assert get_module_config("mymod")["host"] == "v1"

        # Re-bootstrap with different config
        import shutil
        shutil.rmtree(tmp_path / "mymod")
        _make_module(
            tmp_path, "mymod",
            config={"host": {"type": "string"}},
            config_defaults={"host": "v2"},
        )
        # config.json is cached by mtime; both writes can land in the same clock tick.
        config_json = tmp_path / "mymod" / "config.json"
        st = config_json.stat()
        os.utime(config_json, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        assert get_module_config("mymod")["host"] == "v2"


def _make_module_with_router(base_dir: Path, name: str, router_name: str, order: int = 1000) -> None:
    """Create a module with a router class in src/routers/my_router.py."""
    mod_dir = base_dir / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "module.json").write_text(json.dumps({
        "name": name, "version": "1.0.0", "description": "",
    }))
    (mod_dir / "di.json").write_text(json.dumps({
        "routers": [
            {"name": router_name, "class": f"src.routers.{router_name}_router.{router_name.title()}Router", "order": order}
        ],
    }))
    # Create the router Python file
    src_dir = mod_dir / "src" / "routers"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "__init__.py").write_text("")
    (mod_dir / "src" / "__init__.py").write_text("")
    (src_dir / f"{router_name}_router.py").write_text(f'''
class {router_name.title()}Router:
    @property
    def name(self) -> str:
        return "{router_name}"

    def resolve(self, conn, context):
        return None
''')


class TestBootstrapRouters:
    def test_bootstrap_loads_routers_from_di_json(self, tmp_path):
        _make_module_with_router(tmp_path, "testmod", "test", order=100)
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        routers = get_routers()
        assert len(routers) == 1
        assert routers[0].name == "test"

    def test_re_bootstrap_clears_routers(self, tmp_path):
        _make_module_with_router(tmp_path, "testmod", "test", order=100)
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        assert len(get_routers()) == 1
        # Re-bootstrap with no routers
        import shutil
        shutil.rmtree(tmp_path / "testmod")
        _make_module(tmp_path, "testmod")
        bootstrap(core_dir=str(tmp_path), user_dir="/nonexistent")
        assert len(get_routers()) == 0
