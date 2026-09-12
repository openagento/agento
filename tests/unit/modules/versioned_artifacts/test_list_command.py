import argparse
import subprocess

import pytest


def _cmd():
    from agento.modules.versioned_artifacts.src.commands.list import VersionedArtifactListCommand

    return VersionedArtifactListCommand()


def _patch_project(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags",
        lambda _root: ["-f", "docker-compose.yml"], raising=False)


def test_prints_one_row_per_artifact(monkeypatch, capsys, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    stdout = ('{"artifacts": [{"artifact_code": "demo-site", "current_version": "v-20260101-000000-abcd",'
              ' "title": "Demo", "owner": "ops", "created_at": "2026-09-11T08:30:00.000Z",'
              ' "preview_url": "http://localhost:8080/demo-site/"}]}\n')
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""))
    _cmd().execute(argparse.Namespace(actor="admin"))
    out = capsys.readouterr().out
    assert "demo-site" in out and "Demo" in out and "http://localhost:8080/demo-site/" in out
    assert "2026-09-11T08:30:00.000Z" in out


def test_a_valid_json_non_object_reply_prints_an_error_not_a_traceback(monkeypatch, capsys, tmp_path):
    """The class: a well-formed but unexpected line from the other side of
    `docker compose exec` becoming a Python traceback on a command documented never to
    show one. Absence of an error_code is not evidence of success either — a reply with
    no artifact list would otherwise print as "this deployment has no artifacts"."""
    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr=""))
    with pytest.raises(SystemExit) as exc:
        _cmd().execute(argparse.Namespace(actor="admin"))
    assert exc.value.code == 1
    # On stderr, like every other failure this command reports: stdout is the row
    # listing a caller may pipe.
    assert "did not return an artifact list" in capsys.readouterr().err


def test_a_node_stack_on_stderr_is_replaced_by_the_fixed_fallback(monkeypatch, capsys, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    stack = (
        "node:internal/modules/esm/resolve:275\n"
        "    throw new ERR_MODULE_NOT_FOUND(\n"
        "    at packageResolve (node:internal/modules/esm/resolve:275:9)\n"
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stack))
    with pytest.raises(SystemExit):
        _cmd().execute(argparse.Namespace(actor="admin"))
    err = capsys.readouterr().err
    assert "the artifact listing failed" in err
    assert "node:internal" not in err and "at packageResolve" not in err


def test_the_toolbox_is_asked_for_the_list_operation(monkeypatch, tmp_path):
    _patch_project(monkeypatch, tmp_path)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout='{"artifacts": []}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _cmd().execute(argparse.Namespace(actor="admin"))
    assert "--op" in seen["cmd"] and seen["cmd"][seen["cmd"].index("--op") + 1] == "list"
