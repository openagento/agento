"""Every compose exec into the ``cron`` service enters as root, through the launcher.

The cron service holds the credential store in its environment, so an ``exec -u agent
cron …`` hands it to the command directly — no ``env -i``, no fd. The guard asserts over
the argv the CLI actually builds, not over source text, and pins the two ``sandbox``
execs that must keep ``-u agent`` so a later sweep cannot silently take them.
"""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agento.framework.cli._cron_exec import cron_exec

CLI_DIR = Path(__file__).resolve().parents[4] / "src/agento/framework/cli"

STORE_CANARIES = {
    "MYSQL_HOST": "canary-host",
    "MYSQL_PASSWORD": "canary-password",
    "AGENTO_ENCRYPTION_KEY": "canary-key",
    "CONFIG__JIRA__API_TOKEN": "canary-token",
}


def _service_of(argv: list[str]) -> str | None:
    """The compose service an ``exec`` argv targets: the first non-flag after ``exec``."""
    if "exec" not in argv:
        return None
    rest = argv[argv.index("exec") + 1:]
    i = 0
    while i < len(rest):
        token = rest[i]
        if token.startswith("-"):
            # Flags that take a value.
            i += 2 if token in {"-e", "-w", "-u", "--user", "--workdir", "--env"} else 1
            continue
        return token
    return None


# --- the built argv --------------------------------------------------------------------

def _captured(fn, *args, **kwargs) -> list[list[str]]:
    calls: list[list[str]] = []

    def record(argv, *a, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    with patch("subprocess.run", side_effect=record):
        fn(*args, **kwargs)
    return calls


def test_the_universal_cli_proxy_enters_as_root_through_the_launcher():
    from agento.framework.cli import _proxy_to_docker

    with patch("agento.framework.cli._project.find_project_root", return_value=Path("/p")), \
         patch("agento.framework.cli._project.compose_file_flags", return_value=["-f", "/p/c.yml"]), \
         patch("sys.exit"):
        calls = _captured(_proxy_to_docker, ["config:list"])

    argv = calls[0]
    assert _service_of(argv) == "cron"
    assert "agent" not in argv[:argv.index("cron")]
    assert argv[argv.index("cron") + 1] == "/opt/cron-agent/launch.sh"
    assert "--store" in argv
    assert argv[argv.index("--") + 1] == "/opt/cron-agent/run.sh"


def test_prepare_run_enters_as_root_through_the_launcher():
    from agento.framework.cli.run import _fetch_runtime

    result = subprocess.CompletedProcess([], 0, json.dumps({"ok": 1}), "")
    with patch("agento.framework.cli.run.subprocess.run", return_value=result) as run:
        _fetch_runtime(["-f", "/x/c.yml"], "dev_01")
    argv = run.call_args.args[0]
    assert _service_of(argv) == "cron"
    assert argv[argv.index("-u"):argv.index("-u") + 2] == ["-u", "root"]
    assert "/opt/cron-agent/launch.sh" in argv


def test_no_cron_exec_in_the_cli_source_still_names_the_agent_user():
    """Structural sweep over every compose exec the CLI builds."""
    offenders = []
    for path in CLI_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.List):
                continue
            literals = [
                e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if "exec" in literals and "cron" in literals and "agent" in literals:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_sandbox_execs_still_run_as_agent():
    """The sandbox has no launcher and no store — routing it would break every run."""
    source = (CLI_DIR / "run.py").read_text()
    tree = ast.parse(source)
    sandbox_execs = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        literals = [
            e.value for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if "exec" in literals and "sandbox" in literals:
            sandbox_execs += 1
            assert "agent" in literals, "a sandbox exec lost -u agent"
    assert sandbox_execs == 2


# --- the canary ---------------------------------------------------------------------------

@pytest.mark.parametrize("store", [True, False])
def test_a_cron_exec_names_no_store_variable(store):
    """The store reaches the command through the launcher's fd, never through ``-e``."""
    argv = cron_exec(["/opt/cron-agent/run.sh", "config:list"], store=store)
    joined = " ".join(argv)
    for name, value in STORE_CANARIES.items():
        assert name not in joined
        assert value not in joined
    assert ("--store" in argv) is store


def test_the_interactive_setup_upgrade_keeps_its_tty():
    argv = cron_exec(["/opt/cron-agent/run.sh", "setup:upgrade"], tty="-it")
    assert "-it" in argv
    assert argv.index("-it") < argv.index("cron")
