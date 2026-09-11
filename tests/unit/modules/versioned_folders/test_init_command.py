import subprocess

import pytest

from agento.modules.versioned_folders.src.commands.init import collect_source


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


def test_command_is_local_but_still_bootstrapped():
    from agento.framework.cli import _LOCAL_COMMANDS, _LOCAL_MODULE_COMMANDS, _should_proxy
    # Not proxied into cron (which cannot read a host source path)...
    assert not _should_proxy(["versioned-folder:init", "site"])
    # ...but NOT in _LOCAL_COMMANDS, or bootstrap() would be skipped and the
    # module command would never be registered with argparse.
    assert "versioned-folder:init" in _LOCAL_MODULE_COMMANDS
    assert "versioned-folder:init" not in _LOCAL_COMMANDS


def test_resolve_limits_reads_the_configured_values_from_the_toolbox(monkeypatch):
    """The host pre-flight must not carry its own copy of a configurable limit."""
    from agento.modules.versioned_folders.src.commands.init import VersionedFolderInitCommand

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"max_file_size": 20971520, "max_files": 9, "max_total_size": 33}\n',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    limits = VersionedFolderInitCommand._resolve_limits(["-f", "docker-compose.yml"])
    assert limits == {"max_file_size": 20971520, "max_files": 9, "max_total_size": 33}
    assert "--print-limits" in seen["cmd"]


def test_resolve_limits_refuses_to_guess_when_the_toolbox_does_not_answer(monkeypatch):
    """A silent fallback to a default would defeat the point of asking."""
    from agento.modules.versioned_folders.src.commands.init import VersionedFolderInitCommand

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such service"),
    )
    with pytest.raises(SystemExit):
        VersionedFolderInitCommand._resolve_limits(["-f", "docker-compose.yml"])


def test_a_json_scalar_from_the_toolbox_is_not_mistaken_for_a_result(monkeypatch):
    """`json.loads` returns `null`/`[]`/`3` happily, and `.get()` on any of them raises.

    The class: a well-formed but unexpected line from the other side of
    `docker compose exec` becoming a Python traceback on a command documented never
    to show one.
    """
    from agento.modules.versioned_folders.src.commands.init import _last_json_object

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
    from agento.modules.versioned_folders.src.commands.init import _sanitized

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
        "    at createService (file:///app/modules/core/versioned_folders/toolbox/service.js:59:11)\n"
        "Node.js v22.23.2\n"
    )
    assert _sanitized(v8_failure, "fallback") == "fallback"

    # The one shape the toolbox writes on purpose: its own operator log line.
    assert _sanitized("[versioned_folders] ERROR audit insert failed\n", "fallback") == (
        "[versioned_folders] ERROR audit insert failed")
    assert _sanitized("\n\n", "fallback") == "fallback"
    assert len(_sanitized("[versioned_folders] ERROR " + "x" * 5000, "fallback")) == 200


def test_scalar_limits_output_makes_the_command_exit_not_crash(monkeypatch):
    from agento.modules.versioned_folders.src.commands.init import VersionedFolderInitCommand

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="null\n", stderr=""),
    )
    with pytest.raises(SystemExit):
        VersionedFolderInitCommand._resolve_limits(["-f", "docker-compose.yml"])

# ------------------------------------------------------------------ round 6

@pytest.mark.parametrize(
    "stdout",
    [
        "{}\n",                                              # empty object
        "null\n",                                            # a scalar, no object at all
        '{"current_version": "v-20260101-000000-abcd"}\n',   # no folder code
        '{"folder_code": "demo-site"}\n',                    # no version
        '{"folder_code": "other-site", "current_version": "v-20260101-000000-abcd"}\n',
        '{"folder_code": "demo-site", "current_version": "abc123"}\n',   # not a version id
        '{"folder_code": "demo-site", "current_version": null}\n',
        # Python's `$` also matches before a FINAL NEWLINE, so `match()` accepted this
        # and `fullmatch()` does not — an invalid reply reported as success, with its
        # extra line in the terminal.
        '{"folder_code": "demo-site", "current_version": "v-20260101-000000-abcd\\n"}\n',
    ],
)
def test_exit_zero_without_the_success_shape_is_a_failure_not_a_success(monkeypatch, capsys, tmp_path, stdout):
    """The class: absence of an error treated as evidence of success.

    Every case here is exit 0 with no `error_code`, which is exactly what the
    command used to print as `Created folder 'None' at version None` — a folder
    reported to an administrator that nothing created. `_resolve_limits` in the
    same file already asserts its reply field by field; the success reply is now
    held to the same standard.
    """
    import argparse

    from agento.modules.versioned_folders.src.commands.init import VersionedFolderInitCommand

    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags", lambda _root: ["-f", "docker-compose.yml"], raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=""),
    )
    args = argparse.Namespace(folder_code="demo-site", source=None, actor="admin")
    with pytest.raises(SystemExit) as exc:
        VersionedFolderInitCommand().execute(args)
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "Created folder" not in out.out
    assert "did not confirm the folder" in out.err


def test_the_confirmed_success_shape_still_reports_the_folder(monkeypatch, capsys, tmp_path):
    """The other half of the guard: the check must not refuse a real success."""
    import argparse

    from agento.modules.versioned_folders.src.commands.init import VersionedFolderInitCommand

    monkeypatch.setattr(
        "agento.framework.cli._project.find_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "agento.framework.cli._project.compose_file_flags", lambda _root: ["-f", "docker-compose.yml"], raising=False)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"folder_code": "demo-site", "current_version": "v-20260101-000000-abcd"}\n',
            stderr=""),
    )
    args = argparse.Namespace(folder_code="demo-site", source=None, actor="admin")
    VersionedFolderInitCommand().execute(args)
    assert "Created folder 'demo-site' at version v-20260101-000000-abcd" in capsys.readouterr().out
