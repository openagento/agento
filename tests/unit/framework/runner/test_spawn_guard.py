"""TST-2 class guard (WS5): every process start (by AST) outside the runner must be listed
below with its reason; a new vendor CLI spawn in cron goes through ``runner.client``."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[4] / "src" / "agento"

SPAWNS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput",
                   "getstatusoutput"},
    "os": {"fork", "forkpty", "system", "popen", "execv", "execve", "execvp", "execvpe", "execl",
           "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl",
           "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp"},
    "pty": {"spawn", "fork"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}

ALLOWED = {
    # The runner: where the CLIs start.
    "framework/runner/server.py": "the runner's own ops",
    "framework/runner/listen.py": "root socket activation, execs the server",
    "framework/harness/subprocess_runner.py": "the run; constructed only in the runner",
    # Framework CLI in cron (run.sh), which itself goes through the runner.
    "framework/admin/screens/agents.py": "run.sh workspace:build",
    "framework/admin/screens/jobs.py": "run.sh replay",
    # Root crontab renderer.
    "framework/docker/cron/install-crontab.py": "crontab",
    # Host side: docker / docker compose, never in a container.
    "framework/cli/__init__.py": "host docker",
    "framework/cli/_provisioning.py": "host docker build",
    "framework/cli/compose.py": "host docker compose",
    "framework/cli/doctor.py": "host docker",
    "framework/cli/install.py": "host docker, uv",
    "framework/cli/module.py": "host docker compose",
    "framework/cli/run.py": "host docker compose exec sandbox",
    "framework/cli/upgrade.py": "host docker, uv",
    "modules/versioned_artifacts/src/commands/_toolbox.py": "host docker compose exec toolbox",
}


def _spawns(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.attr in SPAWNS.get(node.func.value.id, ())) or (isinstance(node, ast.ImportFrom) and node.module in SPAWNS and any(
                a.name in SPAWNS[node.module] for a in node.names)):
            lines.append(node.lineno)
    return lines


def test_only_listed_files_start_a_process():
    found = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if (lines := _spawns(ast.parse(path.read_text()))) and rel not in ALLOWED:
            found[rel] = lines
    assert found == {}, f"a process started outside the runner: {found}"


def test_the_list_has_no_dead_entry():
    for rel in ALLOWED:
        assert _spawns(ast.parse((SRC / rel).read_text())), f"{rel} starts nothing now"


def test_the_runner_server_imports_no_database_or_store():
    """The runner holds no secret: its own modules import nothing that reads the store."""
    for name in ("server.py", "wire.py", "listen.py"):
        tree = ast.parse((SRC / "framework" / "runner" / name).read_text())
        modules = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in modules if "pymysql" in m or "credential_store" in m
                    or "encryptor" in m}, name
