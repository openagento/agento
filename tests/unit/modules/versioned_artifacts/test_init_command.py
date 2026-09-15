import json
import re
import subprocess
from pathlib import Path

import pytest

from agento.modules.versioned_artifacts.src.commands.init import collect_source


def test_collect_source_walks_files_and_skips_git(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "css").mkdir()
    (tmp_path / "css/style.css").write_text("body{}")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text("secret")
    files = collect_source(tmp_path, max_file_size=1024, max_files=100, max_total_size=10240)
    assert sorted(f["path"] for f in files) == ["css/style.css", "index.html"]


def test_collect_source_refuses_a_symlink(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to("/etc/passwd")
    try:
        collect_source(tmp_path, max_file_size=1024, max_files=100, max_total_size=10240)
    except ValueError as exc:
        assert "SYMLINK_NOT_ALLOWED" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_collect_source_enforces_max_file_size(tmp_path):
    (tmp_path / "big.txt").write_bytes(b"x" * 2048)
    try:
        collect_source(tmp_path, max_file_size=1024, max_files=100, max_total_size=10240)
    except ValueError as exc:
        assert "FILE_TOO_LARGE" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_every_declared_command_is_local_but_still_bootstrapped():
    """The whole class, not one name: every command this module declares must stay on the
    host, and a fourth one added without its `_LOCAL_MODULE_COMMANDS` entry is silently
    proxied into cron, which has no docker socket."""
    import json
    from pathlib import Path

    from agento.framework.cli import _LOCAL_COMMANDS, _LOCAL_MODULE_COMMANDS, _should_proxy

    di = json.loads(
        (Path(__file__).parents[4] / "src/agento/modules/versioned_artifacts/di.json").read_text()
    )
    for name in (c["name"] for c in di["commands"]):
        # Not proxied into cron (which cannot read a host source path)...
        assert not _should_proxy([name, "site"])
        # ...but NOT in _LOCAL_COMMANDS, or bootstrap() would be skipped and the
        # module command would never be registered with argparse.
        assert name in _LOCAL_MODULE_COMMANDS
        assert name not in _LOCAL_COMMANDS


def test_every_invocation_of_this_command_stays_on_the_host():
    """Any name the CLI accepts for it must be in the local set, or it is proxied
    into cron — which has neither the docker socket nor the compose file."""
    from agento.framework.cli import _LOCAL_COMMANDS, _LOCAL_MODULE_COMMANDS
    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand

    cmd = VersionedArtifactInitCommand()
    local = _LOCAL_COMMANDS | _LOCAL_MODULE_COMMANDS
    for invocation in filter(None, (cmd.name, cmd.shortcut)):
        assert invocation in local, f"`agento {invocation}` would be proxied into cron"


def test_the_di_declaration_holds_exactly_the_module_commands():
    """Equality, not membership: a command class whose registration was forgotten, and a
    declared name no class implements, are both `agento <cmd>: command not found`."""
    import json
    from pathlib import Path

    from agento.modules.versioned_artifacts.src.commands.delete import (
        VersionedArtifactDeleteCommand,
    )
    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand
    from agento.modules.versioned_artifacts.src.commands.list import VersionedArtifactListCommand
    from agento.modules.versioned_artifacts.src.commands.publish import VersionedArtifactPublishCommand

    di = json.loads(
        (Path(__file__).parents[4] / "src/agento/modules/versioned_artifacts/di.json").read_text()
    )
    implemented = {
        VersionedArtifactInitCommand().name,
        VersionedArtifactListCommand().name,
        VersionedArtifactPublishCommand().name,
        VersionedArtifactDeleteCommand().name,
    }
    assert {c["name"] for c in di["commands"]} == implemented


def test_resolve_limits_reads_the_configured_values_from_the_toolbox(monkeypatch):
    """The host pre-flight must not carry its own copy of a configurable limit."""
    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"max_file_size": 20971520, "max_files": 9, "max_total_size": 33}\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    limits = VersionedArtifactInitCommand._resolve_limits(["-f", "docker-compose.yml"])
    assert limits == {"max_file_size": 20971520, "max_files": 9, "max_total_size": 33}
    assert "--print-limits" in seen["cmd"]


def test_resolve_limits_refuses_to_guess_when_the_toolbox_does_not_answer(monkeypatch):
    """A silent fallback to a default would defeat the point of asking."""
    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such service"),
    )
    with pytest.raises(SystemExit):
        VersionedArtifactInitCommand._resolve_limits(["-f", "docker-compose.yml"])


def test_a_json_scalar_from_the_toolbox_is_not_mistaken_for_a_result(monkeypatch):
    """`json.loads` returns `null`/`[]`/`3` happily, and `.get()` on any of them raises.

    The class: a well-formed but unexpected line from the other side of
    `docker compose exec` becoming a Python traceback on a command documented never
    to show one.
    """
    from agento.modules.versioned_artifacts.src.commands._toolbox import _last_json_object

    assert _last_json_object("null\n") == {}
    assert _last_json_object("[]\n") == {}
    assert _last_json_object("3\n") == {}
    assert _last_json_object('not json\n{"max_files": 1}\n') == {"max_files": 1}
    # The LAST object wins: the toolbox may log before it answers.
    assert _last_json_object('{"a": 1}\n{"b": 2}\n') == {"b": 2}


def test_a_toolbox_stack_trace_never_reaches_the_operator_verbatim(monkeypatch):
    """A host-side traceback is the same leak the tool layer refuses for the agent.

    An ALLOWLIST, checked against the stack shapes a blocklist kept missing: the
    round-2 filter dropped `at ` / `file://` / `Node.js v` lines and then returned
    the `node:internal/...` frame, the source line and the caret line that a real
    ESM resolution failure starts with.
    """
    from agento.modules.versioned_artifacts.src.commands._toolbox import _sanitized

    esm_failure = (
        "node:internal/modules/esm/resolve:275\n"
        "    throw new ERR_MODULE_NOT_FOUND(\n"
        "          ^\n"
        "\n"
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '/opt/agento-toolbox-src/db.js'\n"
        "    at packageResolve (node:internal/modules/esm/resolve:275:9)\n"
        "Node.js v22.23.2\n"
    )
    assert _sanitized(esm_failure, "fallback") == "fallback"

    v8_failure = (
        "configuration rejected\n"
        "    at createService (file:///app/modules/core/versioned_artifacts/toolbox/service.js:59:11)\n"
        "Node.js v22.23.2\n"
    )
    assert _sanitized(v8_failure, "fallback") == "fallback"

    # The one shape the toolbox writes on purpose: its own operator log line.
    assert _sanitized("[versioned_artifacts] ERROR audit insert failed\n", "fallback") == (
        "[versioned_artifacts] ERROR audit insert failed")
    assert _sanitized("\n\n", "fallback") == "fallback"
    assert len(_sanitized("[versioned_artifacts] ERROR " + "x" * 5000, "fallback")) == 200


def test_scalar_limits_output_makes_the_command_exit_not_crash(monkeypatch):
    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="null\n", stderr=""),
    )
    with pytest.raises(SystemExit):
        VersionedArtifactInitCommand._resolve_limits(["-f", "docker-compose.yml"])

# ------------------------------------------------------------------ round 6

@pytest.mark.parametrize(
    "stdout",
    [
        "{}\n",                                              # empty object
        "null\n",                                            # a scalar, no object at all
        '{"current_version": "v-20260101-000000-abcd"}\n',   # no artifact code
        '{"artifact_code": "demo-site"}\n',                    # no version
        '{"artifact_code": "other-site", "current_version": "v-20260101-000000-abcd"}\n',
        '{"artifact_code": "demo-site", "current_version": "abc123"}\n',   # not a version id
        '{"artifact_code": "demo-site", "current_version": null}\n',
        # Python's `$` also matches before a FINAL NEWLINE, so `match()` accepted this
        # and `fullmatch()` does not — an invalid reply reported as success, with its
        # extra line in the terminal.
        '{"artifact_code": "demo-site", "current_version": "v-20260101-000000-abcd\\n"}\n',
    ],
)
def test_exit_zero_without_the_success_shape_is_a_failure_not_a_success(monkeypatch, capsys, tmp_path, stdout):
    """The class: absence of an error treated as evidence of success.

    Every case here is exit 0 with no `error_code`, which is exactly what the
    command used to print as `Created artifact 'None' at version None` — an artifact
    reported to an administrator that nothing created. `_resolve_limits` in the
    same file already asserts its reply field by field; the success reply is now
    held to the same standard.
    """
    import argparse

    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand

    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags", lambda _root: ["-f", "docker-compose.yml"], raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""),
    )
    args = argparse.Namespace(artifact_code="demo-site", source=None, actor="admin", title=None, owner=None)
    with pytest.raises(SystemExit) as exc:
        VersionedArtifactInitCommand().execute(args)
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "Created artifact" not in out.out
    assert "did not confirm the artifact" in out.err


def test_the_confirmed_success_shape_still_reports_the_artifact(monkeypatch, capsys, tmp_path):
    """The other half of the guard: the check must not refuse a real success."""
    import argparse

    from agento.modules.versioned_artifacts.src.commands.init import VersionedArtifactInitCommand

    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags", lambda _root: ["-f", "docker-compose.yml"], raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"artifact_code": "demo-site", "current_version": "v-20260101-000000-abcd"}\n',
            stderr=""),
    )
    args = argparse.Namespace(artifact_code="demo-site", source=None, actor="admin", title=None, owner=None)
    VersionedArtifactInitCommand().execute(args)
    assert "Created artifact 'demo-site' at version v-20260101-000000-abcd" in capsys.readouterr().out


REPO_ROOT = Path(__file__).parents[4]
MODULE_DIR = REPO_ROOT / "src/agento/modules/versioned_artifacts"
MODULE_DOCS = (
    REPO_ROOT / "docs/modules/versioned-artifacts.md",
    REPO_ROOT / "docs/cli/artifact-init.md",
    REPO_ROOT / "docs/cli/artifact-list.md",
    REPO_ROOT / "docs/cli/artifact-publish.md",
)


def _declared_commands() -> set[str]:
    di = json.loads((MODULE_DIR / "di.json").read_text())
    return {c["name"] for c in di["commands"]}


def _declared_tools() -> set[str]:
    mod = json.loads((MODULE_DIR / "module.json").read_text())
    return {t["name"] for t in mod["tools"]}


def test_docs_name_only_artifact_commands_that_are_registered():
    """Shape defended: a doc states a command name no manifest declares, so the
    documented setup cannot be followed. Scoped to this module's own `artifact:*`
    namespace — framework commands belong to the framework's own tests."""
    declared = _declared_commands()
    offenders = []
    for doc in MODULE_DOCS:
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for cmd in re.findall(r"[a-z][a-z-]*artifact[a-z-]*:[a-z:]+", line):
                if cmd not in declared:
                    offenders.append(f"{doc.relative_to(REPO_ROOT)}:{lineno} names `{cmd}`")
    assert offenders == [], "undeclared commands in the docs: " + "; ".join(offenders)


def test_docs_enable_loop_names_only_declared_tools():
    """Shape defended: the enable instructions name a tool the manifest does not
    declare — notably the plural module name instead of the singular master switch,
    which silently leaves the whole toolset disabled."""
    declared = _declared_tools()
    doc = REPO_ROOT / "docs/modules/versioned-artifacts.md"
    text = doc.read_text()

    loop = re.search(r"for t in (.+?); do", text, re.DOTALL)
    assert loop, "the enable checklist's `for t in ...` loop is gone — update this guard"
    named = set(re.findall(r"versioned_artifact[a-z_]*", loop.group(1)))

    assert named, "the enable loop names no tools"
    assert named <= declared, f"undeclared tools in the enable loop: {sorted(named - declared)}"
    # The loop is the operator's only enable path, so it must cover every tool.
    assert named == declared, f"enable loop misses declared tools: {sorted(declared - named)}"


def test_docs_state_the_configured_storage_root():
    """Shape defended: a documented config default that differs from config.json —
    an operator following it points the store at the bind root, one level above the
    real store, and every existing artifact looks missing."""
    cfg = json.loads((MODULE_DIR / "config.json").read_text())
    doc = (REPO_ROOT / "docs/modules/versioned-artifacts.md").read_text()

    row = re.search(r"\| `versioned_artifacts/storage_root` \| `([^`]+)` \|", doc)
    assert row, "the storage_root row is gone from the config table — update this guard"
    assert row.group(1) == cfg["storage_root"]
