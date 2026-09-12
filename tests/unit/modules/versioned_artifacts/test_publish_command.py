import argparse
import subprocess

import pytest


def _cmd():
    from agento.modules.versioned_artifacts.src.commands.publish import (
        VersionedArtifactPublishCommand,
    )

    return VersionedArtifactPublishCommand()


def _patch_project(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags",
        lambda _root: ["-f", "docker-compose.yml"], raising=False)


def test_expected_is_required_and_never_defaulted():
    """A default would silently disable the optimistic-concurrency guard, which is the
    only thing that stops two publishers from overwriting each other."""
    parser = argparse.ArgumentParser(prog="artifact:publish")
    _cmd().configure(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["demo-site", "v-20260101-000000-abcd"])
    args = parser.parse_args(
        ["demo-site", "v-20260101-000000-abcd", "--expected", "v-20251231-000000-abcd"])
    assert args.expected == "v-20251231-000000-abcd"


def test_a_stale_expected_exits_non_zero_with_the_code_visible(monkeypatch, capsys, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    body = ('{"error_code": "CURRENT_VERSION_CHANGED", "message": "the published version changed;'
            ' re-read the current version and decide again"}\n')
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=body, stderr=""))
    args = argparse.Namespace(artifact_code="demo-site", version_id="v-20260101-000000-abcd",
                              expected="v-20251231-000000-abcd", actor="admin")
    with pytest.raises(SystemExit) as exc:
        _cmd().execute(args)
    assert exc.value.code == 1
    assert "CURRENT_VERSION_CHANGED" in capsys.readouterr().err


def test_a_successful_publish_reports_the_version_and_the_previous_one(monkeypatch, capsys, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    body = ('{"current_version": "v-20260101-000000-abcd",'
            ' "previous_version": "v-20251231-000000-abcd",'
            ' "preview_url": "http://localhost:8080/demo-site/"}\n')
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""))
    args = argparse.Namespace(artifact_code="demo-site", version_id="v-20260101-000000-abcd",
                              expected="v-20251231-000000-abcd", actor="admin")
    _cmd().execute(args)
    out = capsys.readouterr().out
    assert "Published 'demo-site' at version v-20260101-000000-abcd" in out
    assert "v-20251231-000000-abcd" in out
    # The operator publishes in order to be looked at: printing the address is the
    # difference between a confirmation and something to click.
    assert "http://localhost:8080/demo-site/" in out


def test_a_stale_served_tree_prints_the_repair_call(monkeypatch, capsys, tmp_path):
    """The store moved and HTTP did not. The repair is this same command with
    --expected equal to the version just published, so the operator must be told it."""
    _patch_project(monkeypatch, tmp_path)
    body = ('{"current_version": "v-20260101-000000-abcd",'
            ' "previous_version": "v-20251231-000000-abcd", "preview_stale": true}\n')
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=body, stderr=""))
    args = argparse.Namespace(artifact_code="demo-site", version_id="v-20260101-000000-abcd",
                              expected="v-20251231-000000-abcd", actor="admin")
    _cmd().execute(args)
    out = capsys.readouterr().out
    assert "--expected v-20260101-000000-abcd" in out


def test_exit_zero_without_the_confirmed_version_is_a_failure(monkeypatch, capsys, tmp_path):
    """Absence of an error treated as evidence of success — the same class the init
    command was corrected for."""
    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="{}\n", stderr=""))
    args = argparse.Namespace(artifact_code="demo-site", version_id="v-20260101-000000-abcd",
                              expected="v-20251231-000000-abcd", actor="admin")
    with pytest.raises(SystemExit) as exc:
        _cmd().execute(args)
    assert exc.value.code == 1
    assert "did not confirm the publish" in capsys.readouterr().err
