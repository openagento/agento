"""pty_login against a fake CLI: an honest double that prints a URL and a prompt, reads a
line, writes a credentials file, and exits 0 or 1 (TST-3)."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import textwrap

import pytest

from agento.framework.agent_manager.errors import AuthenticationError
from agento.framework.agent_manager.pty_login import PtyLogin, spawn
from agento.framework.harness.protocols import AuthResult, LoginPrompt
from tests.waiting import wait_until

# The CLI runs in a runner (WS5): an in-process server here.
pytestmark = pytest.mark.usefixtures("runner_server")

LONG_URL = "https://login.example.com/oauth/authorize?code=true&state=" + "a" * 400
URL = re.compile(r"https://\S+/oauth/authorize\?\S+")
PROMPT = re.compile(r"Paste code here")

FAKE_CLI = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    print("If the browser didn't open, visit: {LONG_URL}", flush=True)
    print("env-leak" if "AGENTO_ENCRYPTION_KEY" in os.environ else "env-clean", flush=True)
    code = input("Paste code here if prompted > ")
    if code != "good-code#state":
        sys.exit(1)
    path = os.path.join(os.environ["HOME"], "creds.json")
    with open(path, "w") as f:
        json.dump({{"token": "t-" + code}}, f)
""")


@pytest.fixture
def cli(fake_cli) -> str:
    return str(fake_cli("fake-cli", FAKE_CLI))


def _wait_exit(proc, timeout=10):
    wait_until(lambda: proc.exit_code() is not None, timeout, 0.05)
    return proc.exit_code()


def _login(proc, parse):
    return PtyLogin(proc, LoginPrompt(url=LONG_URL, user_code=None, needs_code=True), parse)


def test_reads_the_url_unwrapped_answers_the_prompt_and_exits_0(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTO_ENCRYPTION_KEY", "must-not-reach-the-child")
    home = tmp_path / "home"
    home.mkdir()
    proc = spawn([cli], str(home))
    try:
        m = proc.read_until(URL, timeout=10)
        assert m is not None and m.group(0) == LONG_URL
        assert proc.read_until(PROMPT, timeout=10) is not None
        assert proc.read_until(re.compile("env-clean"), timeout=1) is not None
        proc.write_line("good-code#state")
        assert _wait_exit(proc) == 0
    finally:
        proc.kill()
    assert json.loads((home / "creds.json").read_text()) == {"token": "t-good-code#state"}


def test_a_wrong_code_exits_1(cli, tmp_path):
    proc = spawn([cli], str(tmp_path))
    try:
        assert proc.read_until(PROMPT, timeout=10) is not None
        proc.write_line("bad")
        assert _wait_exit(proc) == 1
    finally:
        proc.kill()


def test_no_match_returns_none_at_timeout(cli, tmp_path):
    proc = spawn([cli], str(tmp_path))
    try:
        assert proc.read_until(re.compile("never printed"), timeout=0.5) is None
    finally:
        proc.kill()
    assert proc.exit_code() is not None


def test_a_missing_command_exits_127(tmp_path):
    proc = spawn([str(tmp_path / "no-such-cli")], str(tmp_path))
    assert _wait_exit(proc) == 127


def _parse_creds(tmp_path):
    return lambda: AuthResult(subscription_key=json.loads((tmp_path / "creds.json").read_text())["token"])


def test_pty_login_poll_is_none_while_running_then_parses_the_credential(cli, tmp_path):
    proc = spawn([cli], str(tmp_path))
    login = _login(proc, _parse_creds(tmp_path))
    try:
        assert proc.read_until(PROMPT, timeout=10)
        assert login.poll() is None
        login.submit_code("good-code#state")
        _wait_exit(proc)
        assert login.poll().subscription_key == "t-good-code#state"
    finally:
        login.close()


def test_pty_login_poll_raises_when_the_cli_fails(cli, tmp_path):
    proc = spawn([cli], str(tmp_path))
    login = _login(proc, _parse_creds(tmp_path))
    try:
        assert proc.read_until(PROMPT, timeout=10)
        login.submit_code("bad")
        _wait_exit(proc)
        with pytest.raises(AuthenticationError):
            login.poll()
    finally:
        login.close()


def test_close_kills_a_running_cli(cli, tmp_path):
    proc = spawn([cli], str(tmp_path))
    try:
        assert proc.read_until(PROMPT, timeout=10)
        _login(proc, lambda: None).close()
        assert proc.exit_code() == -signal.SIGKILL
    finally:
        proc.kill()


def test_a_sigkilled_parent_leaves_no_live_cli(tmp_path, fake_cli):
    """No `finally` runs in a SIGKILLed worker: the master fd closes with it and the CLI,
    the PTY's session leader, gets SIGHUP (and PR_SET_PDEATHSIG on Linux)."""
    pid_file = tmp_path / "cli.pid"
    cli = str(fake_cli("fake-cli", textwrap.dedent(f"""\
        #!{sys.executable}
        import os, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        print("ready", flush=True)
        time.sleep(120)
    """)))
    parent = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""\
        import re, time
        from agento.framework.agent_manager.pty_login import spawn
        p = spawn([{cli!r}], {str(tmp_path)!r})
        p.read_until(re.compile("ready"), 10)
        print("spawned", flush=True)
        time.sleep(120)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout.readline().strip() == "spawned"
        child = int(pid_file.read_text())
        parent.send_signal(signal.SIGKILL)
        parent.wait(5)

        def gone():
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return True
            return False

        if not wait_until(gone, 10, 0.1):
            os.kill(child, signal.SIGKILL)
            pytest.fail("the CLI outlived its SIGKILLed worker")
    finally:
        parent.kill()


def _split_cli(fake_cli, parts: list[bytes]) -> str:
    """A CLI that writes ``parts`` as separate writes, 0.3 s apart, then waits."""
    return str(fake_cli("fake-cli", textwrap.dedent(f"""\
        #!{sys.executable}
        import os, time
        for part in {parts!r}:
            os.write(1, part)
            time.sleep(0.3)
        time.sleep(30)
    """)))


def _module_patterns():
    from agento.modules.claude.src import auth as claude
    from agento.modules.codex.src import auth as codex

    return [
        (claude._LOGIN_URL, [b"visit: https://claude.com/cai/oauth/authorize?code=true&state=first",
                             b"second\r\nPaste code here > "],
         "https://claude.com/cai/oauth/authorize?code=true&state=firstsecond"),
        (codex._DEVICE_URL, [b"   https://auth.openai.com/co", b"dex/device\r\n"], "https://auth.openai.com/codex/device"),
        (codex._DEVICE_CODE, [b"   WXYZ-1234", b"5\r\n"], "WXYZ-12345"),
    ]


@pytest.mark.parametrize(("pattern", "parts", "expected"), _module_patterns())
def test_a_token_split_across_writes_is_read_whole(tmp_path, fake_cli, pattern, parts, expected):
    """Class guard (CLS-1): every vendor login pattern is matched on a growing stream; a match
    that touches the end of what was read so far may still grow, so it is not returned yet."""
    proc = spawn([_split_cli(fake_cli, parts)], str(tmp_path))
    try:
        m = proc.read_until(pattern, timeout=10)
        assert m is not None and m.group(0) == expected
    finally:
        proc.kill()


def test_an_escape_sequence_or_a_utf8_char_split_across_writes_is_decoded_whole(tmp_path, fake_cli):
    parts = [b"\x1b[3", b"2mhttps://claude.com/cai/oauth/authorize?state=\xc5", b"\xbc\x1b[0m\r\n"]
    proc = spawn([_split_cli(fake_cli, parts)], str(tmp_path))
    try:
        m = proc.read_until(re.compile(r"\S+"), timeout=10)
        assert m is not None and m.group(0) == "https://claude.com/cai/oauth/authorize?state=ż"
    finally:
        proc.kill()


def test_a_match_at_the_end_of_the_output_is_returned_when_the_cli_exits(tmp_path, fake_cli):
    cli = str(fake_cli("fake-cli", f"#!{sys.executable}\nimport os\nos.write(1, b'WXYZ-12345')\n"))
    proc = spawn([cli], str(tmp_path))
    m = proc.read_until(re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b"), timeout=10)
    assert m is not None and m.group(0) == "WXYZ-12345"
