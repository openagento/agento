"""Pi transcript reading: located by glob; the bridge init entry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agento.modules.pi.src.transcript_reader import PiTranscriptReader

SESSION = "01931f0e-aaaa-bbbb-cccc-000000000001"


def write_transcript(root, session_id, records, slug="workspace-run-42"):
    d = root / "ws" / "dev" / "b1" / ".pi" / "agent" / "sessions" / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"2026-08-12T09-00-00_{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


@pytest.fixture
def reader(tmp_path):
    return PiTranscriptReader(build_root=tmp_path)


_INIT = [{"type": "custom", "customType": "agento-toolbox-init", "data": {"status": "ok"}}]


class TestLocating:
    def test_finds_the_transcript_by_session_id(self, reader, tmp_path):
        write_transcript(tmp_path, SESSION, _INIT)
        assert reader.read_toolbox_init(SESSION) == {"status": "ok"}

    def test_missing_session_raises(self, reader):
        with pytest.raises(FileNotFoundError):
            reader.read_toolbox_init("nope")

    def test_empty_session_id_raises(self, reader):
        with pytest.raises(FileNotFoundError):
            reader.read_toolbox_init("")

    def test_unparseable_lines_are_skipped(self, reader, tmp_path):
        path = write_transcript(tmp_path, SESSION, _INIT)
        path.write_text("not json\n\n" + path.read_text())
        assert reader.read_toolbox_init(SESSION) == {"status": "ok"}


class TestToolboxInitRecord:
    def test_reads_the_bridge_init_record(self, reader, tmp_path):
        write_transcript(
            tmp_path, SESSION,
            [{"type": "custom", "customType": "agento-toolbox-init",
              "data": {"status": "connected", "tools": ["mcp__toolbox__a"]}}],
        )
        assert reader.read_toolbox_init(SESSION)["status"] == "connected"

    def test_absent_record_yields_none(self, reader, tmp_path):
        write_transcript(tmp_path, SESSION, [{"type": "message", "message": {}}])
        assert reader.read_toolbox_init(SESSION) is None

    def test_a_custom_entry_keyed_by_name_is_NOT_matched(self, reader, tmp_path):
        """`pi.appendEntry` keys entries by `customType`. An earlier version read `name`,
        which never matches, so mcp_init was silently always absent."""
        write_transcript(
            tmp_path, SESSION,
            [{"type": "custom", "name": "agento-toolbox-init", "data": {"status": "x"}}],
        )
        assert reader.read_toolbox_init(SESSION) is None


class TestTheDefaultRootIsTheFrameworksBuildDir:
    """The default root was never exercised, and it was wrong.

    Every test above injects ``build_root=tmp_path``, so all of them passed while the
    production default pointed at ``/var/agento/builds`` — a path that exists in no
    container, read from an ``AGENTO_BUILD_DIR`` variable nothing sets. The reader
    therefore found no transcript in any real deployment, leaving
    ``job.toolbox_mcp_connected`` NULL for every Pi job.
    Found by running a job through the consumer queue, not by any unit test.
    """

    def test_the_default_matches_workspace_paths(self):
        from agento.framework.workspace_paths import BUILD_DIR
        from agento.modules.pi.src.transcript_reader import _build_root

        assert _build_root() == Path(BUILD_DIR)

    def test_the_default_follows_the_workspace_env_var(self, tmp_path, monkeypatch):
        """`AGENTO_WORKSPACE_DIR` is the knob that exists; BUILD_DIR derives from it."""
        monkeypatch.setenv("AGENTO_WORKSPACE_DIR", str(tmp_path / "ws"))
        import importlib

        from agento.framework import workspace_paths
        from agento.modules.pi.src import transcript_reader

        importlib.reload(workspace_paths)
        try:
            assert transcript_reader._build_root() == tmp_path / "ws" / "build"
        finally:
            monkeypatch.delenv("AGENTO_WORKSPACE_DIR", raising=False)
            importlib.reload(workspace_paths)

    def test_a_transcript_under_the_default_root_is_found(self, tmp_path, monkeypatch):
        """End to end through the default: no explicit build_root anywhere."""
        monkeypatch.setattr(
            "agento.modules.pi.src.transcript_reader._build_root",
            lambda: tmp_path,
        )
        write_transcript(tmp_path, SESSION, [
            {"type": "custom", "customType": "agento-toolbox-init",
             "data": {"status": "connected", "tools": ["mcp__toolbox__x"]}},
        ])
        assert PiTranscriptReader().read_toolbox_init(SESSION) == {
            "status": "connected", "tools": ["mcp__toolbox__x"],
        }
