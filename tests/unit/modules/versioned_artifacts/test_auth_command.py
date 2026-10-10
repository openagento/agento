import argparse
import io
import json
import subprocess

import pytest


def _cmd():
    from agento.modules.versioned_artifacts.src.commands.auth import VersionedArtifactAuthCommand

    return VersionedArtifactAuthCommand()


def _parse(argv):
    parser = argparse.ArgumentParser(prog="artifact:auth")
    _cmd().configure(parser)
    return parser.parse_args(argv)


@pytest.fixture
def toolbox(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags",
        lambda _root: ["-f", "docker-compose.yml"], raising=False)
    calls = []
    body = '{"auth_enabled": true, "auth_user": "demo-site", "password": "s3cret-from-stdin"}\n'

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_pass_stdin_sends_the_password_in_the_payload_never_in_argv(monkeypatch, toolbox):
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret-from-stdin\n"))
    _cmd().execute(_parse(["demo-site", "--pass-stdin"]))
    (cmd, kw), = toolbox
    assert "s3cret-from-stdin" not in " ".join(cmd)
    assert json.loads(kw["input"])["password"] == "s3cret-from-stdin"


def test_an_empty_stdin_password_is_refused(monkeypatch, capsys, toolbox):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit) as exc:
        _cmd().execute(_parse(["demo-site", "--pass-stdin"]))
    assert exc.value.code == 1
    assert toolbox == []
    assert "empty" in capsys.readouterr().err


def test_a_password_in_argv_is_refused():
    """`--pass <value>` was removed in v0.17: argv lands in shell history and `ps`."""
    with pytest.raises(SystemExit):
        _parse(["demo-site", "--pass", "x"])


def _toolbox_body(monkeypatch, tmp_path, body):
    monkeypatch.setattr("agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr("agento.framework.cli._project.compose_file_flags",
                        lambda _root: ["-f", "docker-compose.yml"], raising=False)
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(body), stderr=""))


def test_show_prints_the_share_url(monkeypatch, tmp_path, capsys):
    _toolbox_body(monkeypatch, tmp_path, {"auth_enabled": True, "auth_user": "u", "password": "p",
                                          "share_url": "https://t.share.localhost:8443/"})
    _cmd().execute(_parse(["demo-site", "--show"]))
    assert "share:    https://t.share.localhost:8443/" in capsys.readouterr().out


def test_show_says_when_shares_are_not_configured(monkeypatch, tmp_path, capsys):
    _toolbox_body(monkeypatch, tmp_path, {"auth_enabled": True, "auth_user": "u", "password": "p",
                                          "share_url": None})
    _cmd().execute(_parse(["demo-site", "--show"]))
    assert "not configured" in capsys.readouterr().out
