"""Real-CLI contract tests for the Claude Code harness (``AGENTO_E2E=1``).

``test_agento_run_real_cli.py`` proves that a run exits 0. This file proves what Agento
READS from the run: the stream-json init and result events, the session transcript and
its subagent files, resume, and the interactive TUI start. A Claude Code bump that
changes one of these fails here, not silently in production telemetry.

One module-scoped steering of the discovered agent_view (harness claude, provider
anthropic, model haiku, a healthy claude credential forced to win the pool), restored in
teardown — the same discovery and restore helpers as the happy-path suite.
"""
from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import signal
import subprocess
import time

import pytest

from agento.framework.cli._project import compose_file_flags
from agento.framework.cli.run import _fetch_runtime
from agento.framework.harness import HarnessRunContext, RunRequest
from agento.framework.ssh_prelude import wrap_with_ssh_prelude
from agento.modules.claude.src import transcript_reader as cl_tr
from agento.modules.claude.src.command_builder import ClaudeCommandBuilder
from agento.modules.claude.src.output_parser import parse_claude_output
from tests.e2e import test_agento_run_real_cli as base

_VIEW = base._AGENT_VIEW
_HOST_BUILD_DIR = base._PROJECT_ROOT / "workspace" / "build"
_TOOLBOX_PROBE = (
    "Call the mcp__toolbox__jira_search tool exactly once with search_term "
    "'agento-e2e-probe' (user = your email from SOUL.md). Then reply with the word done."
)


def _claude_credential() -> dict | None:
    rows = [c for c in base._healthy_credentials()
            if (c.get("scope") or c.get("agent_type")) == "claude"]
    # oauth first: it is the type whose login state Agento materializes itself.
    rows.sort(key=lambda c: c.get("type") != "oauth")
    return rows[0] if rows else None


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not base._E2E_ENABLED, reason="set AGENTO_E2E=1 to run"),
    pytest.mark.skipif(base._E2E_ENABLED and not _VIEW, reason="no agent_view built"),
]


@pytest.fixture(scope="module")
def claude_view():
    credential = _claude_credential()
    if credential is None:
        pytest.skip("no healthy claude credential registered")
    prior = {p: base._agent_view_override(p)
             for p in ("agent_view/harness", "agent_view/provider", "agent_view/model")}
    try:
        base._agento_ok(["config:set", "agent_view/harness", "claude", "--agent-view", _VIEW])
        base._agento_ok(["config:set", "agent_view/provider", "anthropic", "--agent-view", _VIEW])
        base._agento_ok(["config:set", "agent_view/model", "haiku", "--agent-view", _VIEW])
        base._agento_ok(["credential:set-priority", str(credential["id"]),
                         str(base._FORCE_PRIORITY)])
        yield _VIEW, credential
    finally:
        base._agento(["credential:set-priority", str(credential["id"]),
                      str(credential["priority"])])
        for path, value in prior.items():
            base._restore_override(path, value)


@pytest.fixture
def view(claude_view) -> str:
    return claude_view[0]


@pytest.fixture
def host_reader(monkeypatch):
    """The transcript reader, rooted at the host side of the BUILD_DIR mount."""
    monkeypatch.setattr(cl_tr, "BUILD_DIR", str(_HOST_BUILD_DIR))
    return cl_tr.ClaudeTranscriptReader()


def _run(view: str, prompt: str) -> str:
    res = base._agento(["run", view, prompt], timeout=300)
    assert res.returncode == 0, f"rc={res.returncode}\n{res.stdout[-800:]}{res.stderr[:800]}"
    return res.stdout


def _events(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _init(raw: str) -> dict:
    return next(e for e in _events(raw) if e.get("type") == "system" and e.get("subtype") == "init")


def _expected_cli_version() -> str:
    """The sandbox pin: ``docker/.env`` override, else the di.json default."""
    env = base._PROJECT_ROOT / "docker" / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("CLAUDE_CODE_VERSION="):
                return line.split("=", 1)[1].strip()
    di = json.loads((base._PROJECT_ROOT / "src/agento/modules/claude/di.json").read_text())
    return di["agent_harnesses"][0]["sandbox_package"]["default_range"]


def _resume(view: str, session_id: str) -> subprocess.CompletedProcess:
    """Resume ``session_id`` in a fresh run dir, exactly as the consumer builds the argv."""
    flags = compose_file_flags(base._PROJECT_ROOT)
    # A prompt makes prepare-run return a headless runtime; its own command is unused.
    rt = _fetch_runtime(flags, view, prompt="placeholder")
    home = rt["home"]
    ctx = HarnessRunContext(harness="claude", provider="anthropic", model="haiku",
                            working_dir=rt.get("working_dir") or home, home_dir=home)
    cmd = ClaudeCommandBuilder().headless(ctx, RunRequest(prompt="", session_id=session_id))
    secret_env: dict[str, str] = rt.get("env") or {}
    env_args = [a for k in secret_env for a in ("-e", k)]
    return subprocess.run(
        ["docker", "compose", *flags, "exec", "-T", "-u", "agent", "-e", f"HOME={home}",
         *env_args, "-w", ctx.working_dir, "sandbox", *wrap_with_ssh_prelude(cmd)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300,
        env={**os.environ, **secret_env}, cwd=str(base._PROJECT_ROOT / "docker"),
    )


def test_init_and_result_contract(view):
    raw = _run(view, "Reply with exactly the word: pong")
    init = _init(raw)
    assert [(s["name"], s["status"]) for s in init["mcp_servers"]] == [("toolbox", "connected")], (
        "only the toolbox, connected before init (alwaysLoad); no claude.ai connector"
    )
    assert init["permissionMode"] == "bypassPermissions"
    assert init["claude_code_version"] == _expected_cli_version()

    r = parse_claude_output(raw)
    assert r.session_id and r.num_turns and r.num_turns >= 1
    assert r.input_tokens and r.input_tokens > 0
    assert r.cost_usd is not None
    assert r.mcp_init is not None and r.mcp_init.servers[0].status == "connected"


def test_toolbox_call_is_in_the_transcript(view, host_reader):
    raw = _run(view, _TOOLBOX_PROBE)
    if "mcp__toolbox__jira_search" not in _init(raw)["tools"]:
        pytest.skip(f"jira_search is not enabled for agent_view {view}")
    sid = parse_claude_output(raw).session_id
    names = [u.name for u in host_reader.parse(sid).tool_uses]
    assert "mcp__toolbox__jira_search" in names


def test_subagent_toolbox_call_is_counted(view, host_reader):
    raw = _run(
        view,
        "Use the Agent tool exactly once, in the foreground. Tell the subagent: "
        f"\"{_TOOLBOX_PROBE}\" Do not call any toolbox tool yourself. Then reply done.",
    )
    if "mcp__toolbox__jira_search" not in _init(raw)["tools"]:
        pytest.skip(f"jira_search is not enabled for agent_view {view}")
    sid = parse_claude_output(raw).session_id
    main = cl_tr._find_transcript(sid, _HOST_BUILD_DIR)
    sub_files = sorted((main.parent / sid / "subagents").glob("*.jsonl"))
    assert sub_files, "CLI wrote no subagent transcript — layout changed?"
    assert any(u.name == "mcp__toolbox__jira_search"
               for f in sub_files for u in cl_tr._scan(f)[2]), "the subagent made no toolbox call"
    assert "mcp__toolbox__jira_search" in [u.name for u in host_reader.parse(sid).tool_uses]


def test_background_subagent_results_are_summed(view):
    raw = _run(
        view,
        "Use the Agent tool exactly once with run_in_background set to true; tell the "
        "subagent to reply with the word done. Then reply with the word started.",
    )
    results = [e for e in _events(raw) if e.get("type") == "result"]
    assert len(results) == 2, f"expected one result per turn, got {len(results)}"
    assert parse_claude_output(raw).num_turns == sum(e["num_turns"] for e in results)


def test_resume_continues_the_same_session(view, host_reader):
    first = parse_claude_output(_run(view, "Reply with exactly the word: pong"))
    before = host_reader.parse(first.session_id).total_json_lines

    res = _resume(view, first.session_id)
    assert res.returncode == 0, res.stderr[:800]
    resumed = parse_claude_output(res.stdout)
    assert resumed.session_id == first.session_id
    assert host_reader.parse(first.session_id).total_json_lines > before


def test_resume_of_an_unknown_session_raises_with_the_cli_text(view):
    res = _resume(view, "00000000-1111-2222-3333-444444444444")
    with pytest.raises(RuntimeError, match="No conversation found"):
        parse_claude_output(res.stdout)


def _interactive_screen(view: str, *extra: str, wait: float = 60) -> str:
    """Start ``agento run <view>`` on a pty and return the screen text it shows.

    Teardown sends ``/exit`` so the CLI in the sandbox ends too; a kill is the fallback.
    """
    try:
        pid, fd = pty.fork()
    except OSError as exc:
        pytest.skip(f"no pty available: {exc}")
    if pid == 0:  # child
        os.chdir(base._PROJECT_ROOT)
        os.execvpe(base._AGENTO, [base._AGENTO, "run", view, *extra],
                   {**os.environ, "TERM": "xterm-256color"})
    buf, end = b"", time.time() + wait
    try:
        while time.time() < end and b"for agents" not in buf:
            if select.select([fd], [], [], 0.5)[0]:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    break
    finally:
        _end_session(pid, fd)
    text = buf.decode("utf-8", "replace")
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07", " ", text)


def _end_session(pid: int, fd: int, timeout: float = 20) -> None:
    """``/exit`` the TUI, wait for the process, kill it only if it does not end."""
    try:
        with contextlib.suppress(OSError):
            os.write(fd, b"/exit\r")
        end = time.time() + timeout
        while time.time() < end:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            with contextlib.suppress(OSError):  # drain, so the child never blocks on output
                if select.select([fd], [], [], 0.2)[0]:
                    os.read(fd, 65536)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    finally:
        os.close(fd)


@pytest.mark.parametrize("extra,mode", [((), None), (("--yolo",), "bypass permissions on")])
def test_interactive_opens_straight_to_the_prompt(view, extra, mode):
    """Both 2.1.x start dialogs default to "No, exit": the trust dialog for the run dir,
    and, with --yolo, the bypass-mode confirmation."""
    screen = _interactive_screen(view, *extra)
    assert "for agents" in screen, f"TUI prompt never appeared:\n{screen[-1500:]}"
    assert "trust this folder" not in screen.lower()
    assert "No, exit" not in screen
    if mode:
        assert mode in screen


def test_consumer_job_as_agent_view_records_toolbox_telemetry(claude_view):
    """The consumer path (``Consumer._execute_job``) for a job with an agent_view: the
    init report and the transcript reach ``job.toolbox_mcp_*`` via app_monitor."""
    view, credential = claude_view
    res = base._agento(
        ["e2e", "--credential", str(credential["id"]), "--agent-view", view, "--model", "haiku"],
        timeout=400,
    )
    assert res.returncode == 0 and "ALL PASSED" in res.stdout, (
        "\n".join(line for line in res.stdout.splitlines() if "[FAIL]" in line or "ERROR" in line)
    )
