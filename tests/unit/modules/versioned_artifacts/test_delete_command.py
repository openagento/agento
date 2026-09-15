import argparse
import subprocess

import pytest


def _cmd():
    from agento.modules.versioned_artifacts.src.commands.delete import (
        VersionedArtifactDeleteCommand,
    )

    return VersionedArtifactDeleteCommand()


def _patch_project(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags",
        lambda _root: ["-f", "docker-compose.yml"], raising=False)


def _no_toolbox(monkeypatch):
    def _boom(*_a, **_kw):
        raise AssertionError("the toolbox must not be reached")

    monkeypatch.setattr(subprocess, "run", _boom)


def _args():
    return argparse.Namespace(artifact_code="demo-site", actor="admin")


def test_the_first_option_keeps_the_artifact_and_nothing_is_removed(monkeypatch, capsys, tmp_path):
    """Enter must be safe: option 0 is 'keep it', so a mistaken Return destroys nothing."""
    _patch_project(monkeypatch, tmp_path)
    _no_toolbox(monkeypatch)
    seen = {}

    def _select(prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        return 0

    monkeypatch.setattr("agento.framework.cli.terminal.select", _select)
    _cmd().execute(_args())
    assert "Cancelled" in capsys.readouterr().out
    assert seen["options"][0] == "Keep it"
    assert "demo-site" in seen["prompt"] and "cannot be undone" in seen["prompt"]


def test_a_confirmed_delete_reports_both_roots(monkeypatch, capsys, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr("agento.framework.cli.terminal.select", lambda *_a: 1)
    body = '{"artifact_code": "demo-site", "removed_store": true, "removed_published": true}\n'
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""))
    _cmd().execute(_args())
    out = capsys.readouterr().out
    assert "Deleted 'demo-site'" in out
    assert out.count("removed") >= 2


def test_a_missing_artifact_exits_non_zero_with_the_code_visible(monkeypatch, capsys, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr("agento.framework.cli.terminal.select", lambda *_a: 1)
    body = '{"error_code": "ARTIFACT_NOT_FOUND", "message": "artifact not found"}\n'
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=body, stderr=""))
    with pytest.raises(SystemExit) as exc:
        _cmd().execute(_args())
    assert exc.value.code == 1
    assert "ARTIFACT_NOT_FOUND" in capsys.readouterr().err
