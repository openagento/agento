"""Pi transcript reading: located by glob, tool names verbatim."""
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


def assistant_with_tools(*names):
    return {
        "type": "message",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "name": n, "id": f"call-{i}"}
                for i, n in enumerate(names)
            ],
        },
    }


@pytest.fixture
def reader(tmp_path):
    return PiTranscriptReader(build_root=tmp_path)


class TestLocating:
    def test_finds_the_transcript_by_session_id(self, reader, tmp_path):
        write_transcript(tmp_path, SESSION, [{"type": "session", "id": SESSION}])
        assert reader.parse(SESSION).total_json_lines == 1

    def test_missing_session_raises(self, reader):
        with pytest.raises(FileNotFoundError):
            reader.parse("nope")

    def test_empty_session_id_raises(self, reader):
        with pytest.raises(FileNotFoundError):
            reader.parse("")


class TestToolUses:
    def test_names_are_taken_verbatim_so_telemetry_keeps_working(self, reader, tmp_path):
        """The bridge registers `mcp__toolbox__*` precisely so nothing here translates —
        app_monitor counts `name.startswith('mcp__toolbox__')`."""
        write_transcript(
            tmp_path, SESSION,
            [assistant_with_tools("mcp__toolbox__jira_search", "bash")],
        )
        names = [t.name for t in reader.iter_tool_uses(SESSION)]
        assert names == ["mcp__toolbox__jira_search", "bash"]

    def test_user_messages_contribute_no_tool_uses(self, reader, tmp_path):
        write_transcript(
            tmp_path, SESSION,
            [{"type": "message", "message": {"role": "user", "content": [
                {"type": "toolCall", "name": "not_a_real_call", "id": "x"}]}}],
        )
        assert list(reader.iter_tool_uses(SESSION)) == []


class TestFormatDrift:
    def test_parseable_but_unknown_shapes_signal_drift(self, reader, tmp_path):
        """total_json_lines > 0 with recognized_records == 0 is the canonical
        'Pi changed its transcript format' signal."""
        write_transcript(tmp_path, SESSION, [{"type": "brand_new_shape", "x": 1}] * 3)
        summary = reader.parse(SESSION)
        assert summary.total_json_lines == 3
        assert summary.recognized_records == 0

    def test_unparseable_lines_are_skipped(self, reader, tmp_path):
        path = write_transcript(tmp_path, SESSION, [{"type": "message", "message": {}}])
        path.write_text(path.read_text() + "not json\n\n")
        assert reader.parse(SESSION).total_json_lines == 1


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

    def test_reads_the_model_mismatch_entry(self, reader, tmp_path):
        write_transcript(
            tmp_path, SESSION,
            [{"type": "custom", "customType": "agento-model-mismatch",
              "data": {"actualModel": "other"}}],
        )
        assert reader.read_model_mismatch(SESSION)["actualModel"] == "other"


class TestTheDefaultRootIsTheFrameworksBuildDir:
    """The default root was never exercised, and it was wrong.

    Every test above injects ``build_root=tmp_path``, so all of them passed while the
    production default pointed at ``/var/agento/builds`` — a path that exists in no
    container, read from an ``AGENTO_BUILD_DIR`` variable nothing sets. The reader
    therefore found no transcript in any real deployment, leaving
    ``job.toolbox_mcp_connected`` and ``job.toolbox_mcp_calls`` NULL for every Pi job.
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
