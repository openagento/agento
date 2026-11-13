"""The schedule source registry (AG-67 plan §5.4): di.json `schedule_sources` of enabled modules."""
from __future__ import annotations

import json

from agento.framework.module_loader import ModuleManifest
from agento.framework.schedule import sources

SOURCE = '''
from agento.framework.schedule.sources import FireSpec

class Src:
    name = "acme_src"
    editable = False

    def fire_spec(self, cur, row):
        return FireSpec("acme", "cron", row["source_ref"], None, None, None, 50)

class NotASource:
    pass
'''


def _manifest(tmp_path, decls) -> ModuleManifest:
    mod = tmp_path / "acme"
    (mod / "src").mkdir(parents=True)
    (mod / "src" / "source.py").write_text(SOURCE)
    (mod / "di.json").write_text(json.dumps({"schedule_sources": decls}))
    return ModuleManifest(name="acme", version="1.0.0", description="", path=mod,
                          provides={"schedule_sources": decls})


def test_a_declared_source_is_registered_by_its_name(tmp_path, monkeypatch):
    m = _manifest(tmp_path, [{"name": "acme_src", "class": "src.source.Src"}])
    monkeypatch.setattr(sources, "get_manifests", lambda: [m])
    src = sources.get_source("acme_src")
    assert src is not None and src.editable is False
    assert src.fire_spec(None, {"source_ref": "AI-1"}).reference_id == "AI-1"


def test_a_disabled_module_has_no_source(monkeypatch):
    monkeypatch.setattr(sources, "get_manifests", lambda: [])
    assert sources.get_source("acme_src") is None
    assert sources.load_sources() == {}


def test_a_broken_declaration_is_left_out(tmp_path, monkeypatch, caplog):
    m = _manifest(tmp_path, [{"name": "x", "class": "src.source.NotASource"},
                             {"name": "y", "class": "src.source.Missing"},
                             {"name": "acme_src", "class": "src.source.Src"}])
    monkeypatch.setattr(sources, "get_manifests", lambda: [m])
    assert list(sources.load_sources()) == ["acme_src"]
