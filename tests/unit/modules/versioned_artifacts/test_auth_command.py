"""artifact:auth runs in cron and asks the toolbox `/admin` endpoint (D-MINIAPP-2, O-4)."""
import argparse
import io

import pytest

from agento.modules.versioned_artifacts.src.commands import auth
from agento.web import toolbox_client


def _parse(argv):
    parser = argparse.ArgumentParser(prog="artifact:auth")
    auth.VersionedArtifactAuthCommand().configure(parser)
    return parser.parse_args(argv)


class _Conn:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def toolbox(monkeypatch):
    calls = []
    conn = _Conn()
    answer = {"status": 200, "body": {"auth_enabled": True, "auth_user": "demo-site", "password": "pw",
                                      "share_url": "https://t.share.localhost:8443/"}}

    def admin_request(c, method, path, *, subject_id, body=None, timeout=30.0):
        calls.append((c, method, path, subject_id, body))
        return toolbox_client.InvokeResult(answer["status"], answer["body"])

    monkeypatch.setattr(auth, "get_connection", lambda _cfg: conn)
    monkeypatch.setattr(toolbox_client, "admin_request", admin_request)
    return calls, answer, conn


def test_sets_auth_through_the_admin_endpoint_and_prints_the_password_once(toolbox, capsys):
    calls, _, conn = toolbox
    auth.VersionedArtifactAuthCommand().execute(_parse(["demo-site", "--actor", "ops"]))
    assert calls == [(conn, "POST", "versioned-artifacts/demo-site/auth", "service:cli:ops",
                      {"user": None, "password": None, "disable": False})]
    assert conn.closed
    out = capsys.readouterr().out
    assert "password: pw" in out
    assert "share:    https://t.share.localhost:8443/" in out


def test_pass_stdin_sends_the_password_in_the_body_never_in_the_path(monkeypatch, toolbox):
    calls, _, _ = toolbox
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret-from-stdin\n"))
    auth.VersionedArtifactAuthCommand().execute(_parse(["demo-site", "--pass-stdin"]))
    (_, _, path, subject, body), = calls
    assert "s3cret" not in path + subject
    assert body["password"] == "s3cret-from-stdin"


def test_an_empty_stdin_password_is_refused_before_any_call(monkeypatch, capsys, toolbox):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit) as exc:
        auth.VersionedArtifactAuthCommand().execute(_parse(["demo-site", "--pass-stdin"]))
    assert exc.value.code == 1
    assert toolbox[0] == []
    assert "empty" in capsys.readouterr().err


def test_a_bad_code_never_reaches_the_url(toolbox):
    with pytest.raises(SystemExit):
        auth.VersionedArtifactAuthCommand().execute(_parse(["../miniapps"]))
    assert toolbox[0] == []


def test_disable(toolbox, capsys):
    calls, answer, _ = toolbox
    answer["body"] = {"artifact_code": "demo-site", "auth_enabled": False}
    auth.VersionedArtifactAuthCommand().execute(_parse(["demo-site", "--disable"]))
    assert calls[0][4]["disable"] is True
    assert "disabled" in capsys.readouterr().out


def test_a_toolbox_error_is_one_line_and_exit_1(toolbox, capsys):
    _, answer, _ = toolbox
    answer.update(status=404, body={"error_code": "ARTIFACT_NOT_FOUND", "message": "artifact not found"})
    with pytest.raises(SystemExit) as exc:
        auth.VersionedArtifactAuthCommand().execute(_parse(["demo-site"]))
    assert exc.value.code == 1
    assert capsys.readouterr().err == "Error: ARTIFACT_NOT_FOUND: artifact not found\n"


@pytest.mark.parametrize("argv", [["demo-site", "--pass", "x"], ["demo-site", "--show"]])
def test_removed_flags_are_refused(argv):
    """`--pass` puts a password in argv; `--show` read the password back (removed, O-4)."""
    with pytest.raises(SystemExit):
        _parse(argv)
