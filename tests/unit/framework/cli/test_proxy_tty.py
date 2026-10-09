"""Tests for the host-side docker proxy's TTY / execvp gating.

The proxy at ``_proxy_to_docker`` decides:
  * ``-it`` vs ``-T`` (PTY allocation when forwarding into the cron container)
  * ``os.execvp`` vs ``subprocess.run`` (whether to replace the host process)

For ``credential:register`` we want both to track ``sys.stdin.isatty()`` so that:
  * an operator at a terminal gets an interactive prompt + full PTY (OAuth, getpass)
  * a piped/scripted invocation (CI, file redirect) survives without ``-it``
"""
from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch


def _run_proxy_with_stdin(argv, *, isatty: bool):
    """Invoke _proxy_to_docker with all heavy collaborators stubbed.

    Returns a dict with the captured exec_args from execvp or subprocess.run.
    """
    from agento.framework.cli import _proxy_to_docker

    captured: dict = {}

    def fake_execvp(prog, args):
        captured["mode"] = "execvp"
        captured["prog"] = prog
        captured["args"] = args
        # execvp would not return; raise to break out cleanly in tests
        raise SystemExit(0)

    def fake_run(args, **_kw):
        captured["mode"] = "subprocess"
        captured["args"] = args
        return MagicMock(returncode=0)

    with (
        patch("agento.framework.cli._project.find_project_root",
              return_value="/fake/project"),
        patch("agento.framework.cli._project.compose_file_flags",
              return_value=["-f", "docker-compose.yml"]),
        patch("agento.framework.cli.os.execvp", side_effect=fake_execvp),
        patch("agento.framework.cli.subprocess.run", side_effect=fake_run),
        patch("agento.framework.cli.sys.stdin") as fake_stdin,
    ):
        fake_stdin.isatty.return_value = isatty
        with contextlib.suppress(SystemExit):
            _proxy_to_docker(argv)

    return captured


class TestCredentialRegisterTty:
    def test_tty_stdin_allocates_pty_and_execvps(self):
        captured = _run_proxy_with_stdin(
            ["credential:register", "codex", "foo"], isatty=True
        )
        assert captured["mode"] == "execvp"
        # exec_args[3] = "exec"; PTY flag is right after exec/-u agent
        assert "-it" in captured["args"]
        assert "-T" not in captured["args"]

    def test_piped_stdin_uses_subprocess_and_no_pty(self):
        captured = _run_proxy_with_stdin(
            ["credential:register", "codex", "foo", "--with-api-key"], isatty=False
        )
        assert captured["mode"] == "subprocess"
        assert "-T" in captured["args"]
        assert "-it" not in captured["args"]

    def test_oauth_flow_with_tty_still_works(self):
        """No flags + TTY → interactive OAuth path: PTY + execvp."""
        captured = _run_proxy_with_stdin(
            ["credential:register", "codex", "oauth-codex"], isatty=True
        )
        assert captured["mode"] == "execvp"
        assert "-it" in captured["args"]


class TestCredentialRefreshStillForcesPty:
    """credential:refresh has no flag-based alternative; keep it strictly interactive."""

    def test_refresh_uses_pty_even_when_stdin_not_tty(self):
        # credential:refresh stays in _INTERACTIVE_COMMANDS, so PTY is forced
        # regardless of host stdin. (The command itself sys.exits later if
        # there's no TTY inside the container — that's its own concern.)
        captured = _run_proxy_with_stdin(
            ["credential:refresh", "1"], isatty=False
        )
        assert captured["mode"] == "execvp"
        assert "-it" in captured["args"]
