"""End-to-end checks for the ``pi`` harness against the REAL Pi CLI and a real model.

These are the spike checks, turned into repeatable tests. Every one of them found a
defect that the unit suite could not, which is why they are worth keeping:

* the bridge called ``pi.getAllTools()`` in the extension factory. That is an *action
  method* and throws during extension loading, so **every Pi job exited 1** — while the
  unit test's Pi double implemented it as a working function and stayed green.
* Pi reports the canonical catalogue id, which may carry a ``~`` alias marker, so strict
  model comparison **failed a legitimate run**.
* an unclassified Pi assistant error exits 0 in ``--mode json``, so a failed run was
  recorded as a success.

They run on a **free** OpenRouter model, so the money cost is zero; they still need the
Docker stack, a healthy ``openrouter`` credential and network, hence ``@pytest.mark.e2e``
and the ``AGENTO_E2E=1`` opt-in.

The MCP bridge is exercised against a **mock** MCP server started inside the sandbox, not
the live Toolbox: the real Toolbox's tools touch production Jira/MySQL/Bitbucket, and an
e2e test must not have side effects there. Everything else — Pi, the bridge, the model,
the transport — is real.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parents[2]
_AGENTO = shutil.which("agento") or str(_PROJECT_ROOT / "bin" / "agento")
_E2E_ENABLED = os.environ.get("AGENTO_E2E") == "1"

# Free, and a CONCRETE id rather than a router: `openrouter/free` dispatches to another
# model by design, which the identity guard correctly flags. A concrete id exercises the
# strict path, which is the default configuration.
FREE_MODEL = "nvidia/nemotron-3.5-lightning:free"


def _compose_container(service: str) -> str | None:
    """Resolve a running container for a compose service in THIS project.

    Hardcoding `agento-3-sandbox-1` ties the test to one checkout and, worse, could address
    a different stack on a machine running several. Ask compose instead, honouring
    COMPOSE_PROJECT_NAME, and fall back to a label filter.
    """
    for compose_file in ("docker-compose.dev.yml", "docker-compose.yml"):
        path = _PROJECT_ROOT / "docker" / compose_file
        if not path.is_file():
            continue
        res = subprocess.run(
            ["docker", "compose", "-f", str(path), "ps", "-q", service],
            capture_output=True, text=True, timeout=60,
        )
        cid = res.stdout.strip().splitlines()
        if res.returncode == 0 and cid and cid[0]:
            return cid[0]
    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if project:
        res = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
             "--filter", f"label=com.docker.compose.service={service}"],
            capture_output=True, text=True, timeout=60,
        )
        cid = res.stdout.strip().splitlines()
        if cid and cid[0]:
            return cid[0]
    return None


SANDBOX = _compose_container("sandbox") if _E2E_ENABLED else None
CRON = _compose_container("cron") if _E2E_ENABLED else None

# A raw MCP `inputSchema` — nested object, string enum, optional field. Spike S1 asked
# whether this works directly as `ToolDefinition.parameters`; it does, so there is no
# conversion layer to test around.
_RAW_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "the search text"},
        "mode": {"type": "string", "enum": ["fast", "thorough"]},
        "nested": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        "optional_note": {"type": "string"},
    },
    "required": ["query"],
}

_MOCK_SERVER = """
import http from 'node:http';
const SESSION = 'e2e-pi-session';
const SCHEMA = {SCHEMA_JSON};
const sse = (o) => `event: message\\ndata: ${JSON.stringify(o)}\\n\\n`;
let calls = 0;
http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    if (req.method === 'DELETE') { res.writeHead(200).end(''); return; }
    let m = {}; try { m = JSON.parse(body); } catch {}
    if (m.method === 'initialize') {
      res.writeHead(200, {'content-type':'text/event-stream','mcp-session-id':SESSION});
      res.end(sse({jsonrpc:'2.0',id:m.id,result:{protocolVersion:'2025-06-18'}}));
    } else if (m.method === 'notifications/initialized') {
      res.writeHead(202).end('');
    } else if (m.method === 'tools/list') {
      res.writeHead(200,{'content-type':'text/event-stream'});
      res.end(sse({jsonrpc:'2.0',id:m.id,result:{tools:[
        {name:'e2e_probe',description:'echo the query back',inputSchema:SCHEMA,execution:'remote'}]}}));
    } else if (m.method === 'tools/call') {
      calls += 1;
      console.error('MOCK_TOOL_CALL ' + JSON.stringify(m.params?.arguments));
      res.writeHead(200,{'content-type':'text/event-stream'});
      res.end(sse({jsonrpc:'2.0',id:m.id,result:{content:[{type:'text',text:'PROBE_RESULT_42'}]}}));
    } else { res.writeHead(200,{'content-type':'application/json'}).end('{}'); }
  });
}).listen(3998, () => console.error('MOCK_READY'));
""".replace("{SCHEMA_JSON}", json.dumps(_RAW_SCHEMA))


def _docker(args: list[str], *, timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )


def _stack_up() -> bool:
    return bool(SANDBOX) and bool(CRON)


def _pi_available() -> bool:
    return _docker(["exec", SANDBOX, "sh", "-c", "command -v pi"], timeout=30).returncode == 0


def _openrouter_key() -> str | None:
    """Read the key out of the pool via the CLI's own container, never from argv."""
    script = (
        "import sys; sys.path.insert(0,'/opt/agento-src')\n"
        "from agento.framework.agent_manager.credential_store import list_credentials\n"
        "from agento.framework.cli.runtime import _load_framework_config\n"
        "from agento.framework.db import get_connection\n"
        "db,_,_ = _load_framework_config()\n"
        "conn = get_connection(db)\n"
        "try:\n"
        "    rows = [c for c in list_credentials(conn)\n"
        "            if c.scope == 'openrouter' and c.type == 'openrouter_api_key'\n"
        "            and getattr(c, 'enabled', True)\n"
        "            and str(getattr(c.status, 'value', c.status)).lower() == 'ok']\n"
        "    rows.sort(key=lambda c: (c.priority, c.id))\n"
        "    print((rows[0].credentials or {}).get('api_key','') if rows else '')\n"
        "finally:\n"
        "    conn.close()\n"
    )
    # `docker exec -i` needs the script on stdin.
    res = subprocess.run(
        ["docker", "exec", "-i", CRON, "python3", "-"],
        input=script, capture_output=True, text=True, timeout=60,
    )
    key = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    return key or None


_KEY = _openrouter_key() if _E2E_ENABLED and _stack_up() else None

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _E2E_ENABLED,
        reason="set AGENTO_E2E=1 to run (needs the Docker stack, a credential and network)",
    ),
    pytest.mark.skipif(
        _E2E_ENABLED and not _stack_up(),
        reason="the compose sandbox/cron services are not running — start the stack",
    ),
    pytest.mark.skipif(
        _E2E_ENABLED and _stack_up() and not _pi_available(),
        reason="the pi CLI is not in the sandbox image — rebuild it",
    ),
    pytest.mark.skipif(
        _E2E_ENABLED and _stack_up() and not _KEY,
        reason="no HEALTHY, enabled openrouter credential in the pool",
    ),
]


def _bridge_source() -> str:
    return (
        _PROJECT_ROOT / "src" / "agento" / "modules" / "pi" / "bridge" / "agento-toolbox.js"
    ).read_text()


def _run_pi(
    *,
    workdir: str,
    prompt: str,
    model: str = FREE_MODEL,
    with_bridge: bool = False,
    bridge_url: str = "http://127.0.0.1:3998/mcp",
    extra_flags: str = "",
    with_mock: bool = False,
) -> tuple[int, list[dict], str, str]:
    """Run the real Pi CLI in the sandbox. Returns (rc, ndjson events, stderr, mock log)."""
    setup = [f"rm -rf {workdir}", f"mkdir -p {workdir}/.pi", f"cd {workdir}"]
    if with_bridge:
        subprocess.run(
            ["docker", "exec", "-i", SANDBOX, "sh", "-c", f"mkdir -p {workdir}/.pi && cat > {workdir}/.pi/agento-toolbox.js"],
            input=_bridge_source(), capture_output=True, text=True, timeout=60,
        )
        cfg = json.dumps({"url": bridge_url})
        subprocess.run(
            ["docker", "exec", "-i", SANDBOX, "sh", "-c", f"cat > {workdir}/.pi/agento-toolbox.json"],
            input=cfg, capture_output=True, text=True, timeout=60,
        )
        setup = [f"cd {workdir}"]

    if with_mock:
        subprocess.run(
            ["docker", "exec", "-i", SANDBOX, "sh", "-c", "cat > /tmp/e2e_mock_mcp.js"],
            input=_MOCK_SERVER, capture_output=True, text=True, timeout=60,
        )

    bridge_flag = f"-e {workdir}/.pi/agento-toolbox.js" if with_bridge else ""
    mock_start = "node /tmp/e2e_mock_mcp.js 2>/tmp/mock.err & MOCK=$!; sleep 1;" if with_mock else ""
    mock_stop = "kill $MOCK 2>/dev/null;" if with_mock else ""
    script = (
        f"{'; '.join(setup)}; {mock_start} "
        f"printf '%s' \"$PI_PROMPT\" | timeout 180 pi --mode json --offline --no-extensions "
        f"{bridge_flag} {extra_flags} --provider openrouter --model '{model}' "
        f"> out.json 2> err.txt; echo \"RC=$?\"; {mock_stop} "
        f"cat out.json; echo '===STDERR==='; cat err.txt; "
        f"echo '===MOCK==='; cat /tmp/mock.err 2>/dev/null || true"
    )
    # Name-only `-e KEY`, with the values in the docker client's CHILD ENV.
    #
    # `-e KEY=value` puts the decrypted key in this process's argv, where `ps` and
    # /proc expose it for the lifetime of the call. That is the leak the project's
    # name-only convention exists to prevent (see `cli/run.py`, which passes secrets the
    # same way), and an earlier version of this file did exactly that while its own
    # helper docstring claimed "never from argv".
    child_env = {**os.environ, "OPENROUTER_API_KEY": _KEY or "", "PI_PROMPT": prompt}
    res = subprocess.run(
        ["docker", "exec", "-e", "OPENROUTER_API_KEY", "-e", "PI_PROMPT",
         SANDBOX, "sh", "-c", script],
        capture_output=True, text=True, timeout=300, env=child_env,
    )
    out = res.stdout
    rc_line = next((ln for ln in out.splitlines() if ln.startswith("RC=")), "RC=99")
    rc = int(rc_line.split("=", 1)[1])
    body, _, rest = out.partition("===STDERR===")
    stderr, _, mock = rest.partition("===MOCK===")
    events = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return rc, events, stderr, mock


def _assistant_messages(events: list[dict]) -> list[dict]:
    return [
        e["message"] for e in events
        if e.get("type") == "message_end"
        and isinstance(e.get("message"), dict)
        and e["message"].get("role") == "assistant"
    ]


def _mcp_tool_calls(events: list[dict]) -> list[str]:
    """Every ``mcp__toolbox__*`` invocation in the stream.

    This is the same prefix ``app_monitor`` counts for ``job.toolbox_mcp_calls``, so a
    non-zero count here is what makes that telemetry meaningful.
    """
    names: list[str] = []
    for msg in _assistant_messages(events):
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "toolCall":
                name = block.get("name")
                if isinstance(name, str) and name.startswith("mcp__toolbox__"):
                    names.append(name)
    return names


class TestS2EnvAuthentication:
    """S2: `OPENROUTER_API_KEY` in the environment is enough — no auth.json, no /login."""

    def test_a_free_model_run_succeeds_from_env_alone(self):
        rc, events, stderr, _ = _run_pi(workdir="/tmp/e2e_s2", prompt="Reply with exactly: OK")
        assert rc == 0, f"pi failed: {stderr[:500]}"
        assistants = _assistant_messages(events)
        assert assistants, "no assistant message in the stream"
        assert assistants[-1].get("provider") == "openrouter"
        assert assistants[-1].get("stopReason") != "error", assistants[-1].get("errorMessage")

    def test_the_model_that_ran_is_the_model_requested(self):
        """Guards the `~` alias defect from the other direction: a concrete id must come
        back unchanged, so a mismatch really does mean a substitution."""
        _, events, _, _ = _run_pi(workdir="/tmp/e2e_s2b", prompt="Reply with exactly: OK")
        models = {m.get("model") for m in _assistant_messages(events) if m.get("model")}
        assert models, "no model reported on any assistant message"
        assert models == {FREE_MODEL}, f"expected only {FREE_MODEL}, saw {models}"


class TestS1BridgeRegistersAndCallsTools:
    """S1: a raw MCP `inputSchema` works directly as a Pi tool parameter schema."""

    def test_the_bridge_loads_and_the_model_calls_the_tool(self):
        rc, events, stderr, mock = _run_pi(
            workdir="/tmp/e2e_s1",
            prompt=(
                "Call the mcp__toolbox__e2e_probe tool with query=\"hello\" and mode=\"fast\". "
                "Then reply with exactly: DONE"
            ),
            with_bridge=True,
            with_mock=True,
        )
        assert rc == 0, f"pi failed: {stderr[:600]}"
        # The extension loaded at all — this is what `pi.getAllTools()` in the factory broke.
        assert "Failed to load extension" not in stderr
        assert "MOCK_READY" in mock

        calls = _mcp_tool_calls(events)
        assert calls, (
            "the model never called the bridged tool; registration or the schema failed. "
            f"stderr={stderr[:400]}"
        )
        assert calls[0] == "mcp__toolbox__e2e_probe"
        # And the upstream server really received a typed call — proving the raw JSON
        # Schema round-tripped, not merely that a name was registered.
        assert "MOCK_TOOL_CALL" in mock
        assert '"query":"hello"' in mock.replace(" ", "")

    def test_mcp_tool_calls_are_countable_for_telemetry(self):
        """`job.toolbox_mcp_calls` counts exactly this prefix, so it must be non-zero for
        a run that used a Toolbox tool."""
        _, events, _, mock = _run_pi(
            workdir="/tmp/e2e_s1b",
            prompt=(
                "Call the mcp__toolbox__e2e_probe tool once with query=\"count\". "
                "Then reply with exactly: DONE"
            ),
            with_bridge=True,
            with_mock=True,
        )
        assert len(_mcp_tool_calls(events)) >= 1
        assert mock.count("MOCK_TOOL_CALL") >= 1


class TestFailureModesAreNotSilent:
    """Pi signals failure by omission — `--mode json` exits 0 on an assistant error — so
    every guard has to rest on a positive signal."""

    def test_an_unreachable_toolbox_prevents_the_run_entirely(self):
        rc, events, stderr, _ = _run_pi(
            workdir="/tmp/e2e_fail",
            prompt="say hi",
            with_bridge=True,
            bridge_url="http://127.0.0.1:9/mcp",   # nothing listens on port 9
        )
        assert rc != 0, "an unreachable Toolbox must not produce a successful run"
        assert "Failed to load extension" in stderr
        assert not _assistant_messages(events), (
            "the run must not start at all — proceeding with zero tools and reporting "
            "success is the failure mode this prevents"
        )

    def test_no_builtin_tools_leaves_only_bridged_tools(self):
        _, events, _stderr, _mock = _run_pi(
            workdir="/tmp/e2e_nbt",
            prompt="List the names of every tool you can call, one per line. Do not call any.",
            with_bridge=True,
            with_mock=True,
            extra_flags="--no-builtin-tools",
        )
        text = " ".join(
            block.get("text", "")
            for msg in _assistant_messages(events)
            for block in msg.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        assert "mcp__toolbox__e2e_probe" in text, f"bridged tool not visible: {text[:300]}"
        for builtin in ("bash", "edit", "write"):
            assert builtin not in text.split(), (
                f"built-in {builtin!r} still offered despite --no-builtin-tools: {text[:300]}"
            )
