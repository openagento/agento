"""Tests for the root crontab renderer (``docker/cron/install-crontab.py``).

The renderer runs as root, so every one of its inputs must be something uid ``agent``
cannot write, and every line it emits must be argv it built itself.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

RENDERER_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/agento/framework/docker/cron/install-crontab.py"
)


def _docstrings(tree) -> set[str]:
    return {
        ast.get_docstring(n, clean=False) or ""
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
    }


def _load_renderer():
    spec = importlib.util.spec_from_file_location("install_crontab", RENDERER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["install_crontab"] = module
    spec.loader.exec_module(module)
    return module


renderer = _load_renderer()


@pytest.fixture
def modules(tmp_path):
    """An installed-module set the renderer discovers, with no ``modules.json`` anywhere."""
    root = tmp_path / "modules"
    root.mkdir()
    return root


def _module(root: Path, name: str, jobs: list[dict] | str) -> Path:
    d = root / name
    d.mkdir()
    (d / "module.json").write_text(json.dumps({"name": name}))
    if jobs is not None:
        (d / "cron.json").write_text(
            jobs if isinstance(jobs, str) else json.dumps({"jobs": jobs})
        )
    return d


def _render(module_dirs: list[Path], schedule_jobs=None):
    with patch.object(renderer, "iter_module_dirs", return_value=module_dirs), \
         patch.object(renderer, "resolve_module_root", return_value=None), \
         patch.object(renderer, "_schedule_jobs", return_value=schedule_jobs or []):
        return renderer.render()


def _job_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if line and not line.startswith("#") and "install-crontab.py" not in line
        and "=" not in line.split(" ")[0]
    ]


# --- P3: no agent-writable input decides anything -------------------------------------

def test_the_renderer_never_consults_the_agent_writable_module_status(modules):
    """``app/etc/modules.json`` sits on a writable mount — root must not read it."""
    tree = ast.parse(RENDERER_PATH.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "iter_enabled_module_dirs" not in names
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert not any("modules.json" in lit for lit in literals - _docstrings(tree))

    read: list[str] = []
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes

    def spy_text(self, *a, **kw):
        read.append(str(self))
        return real_read_text(self, *a, **kw)

    def spy_bytes(self, *a, **kw):
        read.append(str(self))
        return real_read_bytes(self, *a, **kw)

    with patch.object(Path, "read_text", spy_text), patch.object(Path, "read_bytes", spy_bytes):
        _render([_module(modules, "alpha", [
            {"name": "a", "schedule": "0 * * * *", "command": "a:b"},
        ])])
    assert not any("modules.json" in p for p in read)


def test_a_stray_needs_store_field_changes_nothing(modules):
    plain = _module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "command": "a:b"},
    ])
    with_flag = _module(modules, "beta", [
        {"name": "a", "schedule": "0 * * * *", "command": "a:b", "needs_store": True},
    ])
    a = _render([plain]).replace("alpha", "X")
    b = _render([with_flag]).replace("beta", "X")
    assert a == b


def test_the_store_flag_follows_the_executable_not_the_declaration(modules):
    """``run.sh`` jobs get the store; ``logrotate`` (the framework raw_command) does not."""
    out = _render([_module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "command": "a:b"},
    ])])
    module_line = next(ln for ln in out.splitlines() if "cron:run" in ln)
    logrotate_line = next(ln for ln in out.splitlines() if "logrotate" in ln and ln[0].isdigit())
    assert " --store -- " in module_line
    assert "--store" not in logrotate_line


# --- Discovery ------------------------------------------------------------------------

def test_a_module_without_cron_json_contributes_nothing_and_is_not_an_error(modules):
    d = modules / "alpha"
    d.mkdir()
    (d / "module.json").write_text("{}")
    out = _render([d])
    assert "alpha" not in out


def test_an_empty_jobs_list_renders_nothing(modules):
    out = _render([_module(modules, "alpha", [])])
    assert "alpha" not in out


# --- Parsing --------------------------------------------------------------------------

def test_a_quoted_argument_survives_split_and_quote(modules):
    out = _render([_module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "command": "a:b --msg 'two words'"},
    ])])
    line = next(ln for ln in out.splitlines() if "cron:run" in ln)
    assert "'two words'" in line


def test_an_empty_command_is_rejected(modules):
    out = _render([_module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "command": "   "},
    ])])
    assert "alpha" not in out


# --- Per-source isolation -------------------------------------------------------------

def test_a_malformed_cron_json_removes_only_that_modules_lines(modules):
    bad = _module(modules, "broken", "{not json")
    good = _module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "command": "a:b"},
    ])
    out = _render([bad, good], schedule_jobs=[
        renderer.Job("AG-1: x", "5 * * * *", (renderer.RUN_SH, "cron:run", "m", "publish")),
    ])
    assert "broken" not in out
    assert "cron:run alpha a:b" in out
    assert "AG-1" in out
    assert "logrotate" in out  # framework source unaffected


def test_a_module_raw_command_is_rejected(modules):
    out = _render([_module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "raw_command": "/bin/sh -c evil"},
    ])])
    assert "alpha" not in out
    assert "evil" not in out


def test_the_frameworks_own_logrotate_still_renders(modules):
    out = _render([])
    assert "/usr/sbin/logrotate" in out


# --- Schedule from a {module/path} config directive ----------------------------------

def _render_with_config(module_dirs, env=None, db=None):
    with patch.object(renderer, "_config_overrides", return_value=(env or {}, db or {})):
        return _render(module_dirs)


def test_a_schedule_directive_resolves_from_the_db(modules):
    m = _module(modules, "crm", [{"name": "sync", "schedule": "{crm/sync_schedule}", "command": "sync"}])
    out = _render_with_config([m], db={"crm/sync_schedule": ("*/5 * * * *", False)})
    assert any(line.startswith("*/5 * * * * ") for line in _job_lines(out))


def test_env_wins_over_db_for_a_schedule_directive(modules):
    m = _module(modules, "crm", [{"name": "sync", "schedule": "{crm/sync_schedule}", "command": "sync"}])
    out = _render_with_config(
        [m],
        env={"CONFIG__CRM__SYNC_SCHEDULE": "0 2 * * *"},
        db={"crm/sync_schedule": ("*/5 * * * *", False)},
    )
    assert any(line.startswith("0 2 * * * ") for line in _job_lines(out))


def test_a_schedule_directive_falls_back_to_config_json(modules):
    m = _module(modules, "crm", [{"name": "sync", "schedule": "{crm/sync_schedule}", "command": "sync"}])
    (m / "config.json").write_text(json.dumps({"sync_schedule": "15 * * * *"}))
    out = _render_with_config([m])
    assert any(line.startswith("15 * * * * ") for line in _job_lines(out))


def test_a_literal_schedule_never_loads_config(modules):
    m = _module(modules, "crm", [{"name": "sync", "schedule": "0 * * * *", "command": "sync"}])
    with patch.object(renderer, "_config_overrides", side_effect=AssertionError("loaded")):
        out = _render([m])
    assert any(line.startswith("0 * * * * ") for line in _job_lines(out))


@pytest.mark.parametrize("db", [{}, {"crm/sync_schedule": ("", False)}, {"crm/sync_schedule": ("x", True)}])
def test_an_unset_or_encrypted_directive_omits_only_that_module(modules, db, capsys):
    bad = _module(modules, "crm", [{"name": "sync", "schedule": "{crm/sync_schedule}", "command": "sync"}])
    good = _module(modules, "ok", [{"name": "tick", "schedule": "0 * * * *", "command": "tick"}])
    out = _render_with_config([bad, good], db=db)
    lines = _job_lines(out)
    assert not any("crm" in line for line in lines)
    assert any("cron:run ok tick" in line for line in lines)
    assert "crm/sync_schedule" in capsys.readouterr().err


def test_a_directive_resolving_to_a_bad_expression_is_rejected(modules):
    m = _module(modules, "crm", [{"name": "sync", "schedule": "{crm/sync_schedule}", "command": "sync"}])
    out = _render_with_config([m], db={"crm/sync_schedule": ("* * * * * ; rm -rf /", False)})
    assert not any("crm" in line for line in _job_lines(out))


def test_a_config_database_failure_leaves_the_crontab_untouched(modules):
    renderer._config_overrides.cache_clear()
    m = _module(modules, "crm", [{"name": "sync", "schedule": "{crm/sync_schedule}", "command": "sync"}])
    with patch.object(renderer, "iter_module_dirs", return_value=[m]), \
         patch.object(renderer, "resolve_module_root", return_value=None), \
         patch.object(renderer, "_schedule_jobs", return_value=[]), \
         patch.object(renderer, "store_env") as store_env, \
         patch.object(Path, "read_bytes", return_value=b""), \
         patch.object(renderer, "_connect", side_effect=renderer.OperationalError("down")), \
         patch("subprocess.run") as run:
        store_env.parse.return_value = {}
        assert renderer.main() == 0
    renderer._config_overrides.cache_clear()
    run.assert_not_called()


# --- Dynamic rows ---------------------------------------------------------------------

def test_a_schedule_row_renders_through_the_dispatcher(modules):
    job = renderer.Job(
        "AG-7: nightly", "0 3 * * *",
        (renderer.RUN_SH, "cron:run", "jira_periodic_tasks", "publish", "jira-cron", "AG-7"),
    )
    out = _render([], schedule_jobs=[job])
    line = next(ln for ln in out.splitlines() if "AG-7" in ln and ln[0].isdigit())
    assert "cron:run jira_periodic_tasks publish jira-cron AG-7" in line


def test_a_disabled_module_is_still_rendered_through_the_dispatcher(modules):
    """Root renders from the *installed* catalog; ``cron:run`` gates it after the drop."""
    out = _render([_module(modules, "alpha", [
        {"name": "a", "schedule": "0 * * * *", "command": "a:b"},
    ])])
    assert f"{renderer.RUN_SH} cron:run alpha a:b" in out


# --- Injection ------------------------------------------------------------------------

def test_a_newline_in_a_summary_reaches_only_a_comment():
    job_comment = renderer._comment("AG-1: fine\n0 * * * * /bin/sh -c evil")
    assert "\n" not in job_comment


def test_an_injected_summary_creates_no_line(modules):
    summary = "x\n0 * * * * /bin/sh -c evil"
    job = renderer.Job(
        renderer._comment(f"AG-1: {summary}"),
        "0 4 * * *",
        renderer._validate_argv([renderer.RUN_SH, "cron:run", "m", "publish", "AG-1"]),
    )
    out = _render([], schedule_jobs=[job])
    assert "/bin/sh -c evil" in out              # it survives, but only inside the comment
    for line in out.splitlines():
        if "/bin/sh -c evil" in line:
            assert line.startswith("#")


def test_a_schedule_with_a_shell_metacharacter_is_rejected():
    with pytest.raises(renderer.ValidationError):
        renderer._validate_schedule("* * * * * ; /bin/sh")


def test_a_percent_in_an_argv_item_is_rejected():
    """cron turns ``%`` into a newline on stdin before any shell quoting applies."""
    with pytest.raises(renderer.ValidationError):
        renderer._validate_argv([renderer.RUN_SH, "publish", "a%b"])


def test_an_invalid_schedule_row_omits_only_that_row(modules):
    good = renderer.Job("AG-2: ok", "0 5 * * *", (renderer.RUN_SH, "cron:run", "m", "p"))
    out = _render([_module(modules, "alpha", [
        {"name": "a", "schedule": "not a schedule", "command": "a:b"},
    ])], schedule_jobs=[good])
    assert "alpha" not in out
    assert "AG-2" in out


# --- Failure policy -------------------------------------------------------------------

def test_a_database_failure_leaves_the_crontab_untouched():
    with patch.object(renderer, "_mysql_settings", return_value={"MYSQL_HOST": "nope"}), \
         patch("pymysql.connect", side_effect=OSError("connection refused")), \
         patch.object(renderer, "current_crontab", return_value="KEEP ME\n") as current, \
         patch("subprocess.run") as run:
        assert renderer.main() == 0
    current.assert_not_called()
    run.assert_not_called()


def test_an_unreadable_env_file_leaves_the_crontab_untouched(tmp_path):
    with patch.object(renderer, "ENV_FILE", str(tmp_path / "absent")), \
         patch("subprocess.run") as run:
        with pytest.raises(renderer.OperationalError):
            renderer._mysql_settings()
        assert renderer.main() == 0
    run.assert_not_called()


def test_a_validation_failure_still_writes_the_crontab(modules):
    written: list[str] = []

    def fake_run(argv, **kw):
        if argv[:2] == ["crontab", "-"]:
            written.append(kw["input"])
        return subprocess.CompletedProcess(argv, 1, "", "")

    with patch.object(renderer, "iter_module_dirs", return_value=[_module(modules, "broken", "{bad")]), \
         patch.object(renderer, "resolve_module_root", return_value=None), \
         patch.object(renderer, "_schedule_jobs", return_value=[]), \
         patch.object(renderer.subprocess, "run", side_effect=fake_run):
        assert renderer.main() == 0
    assert written and "logrotate" in written[0]


# --- Credentials ----------------------------------------------------------------------

def test_only_mysql_fields_are_taken_from_the_env_file(tmp_path):
    env = tmp_path / "env"
    env.write_bytes(
        b"MYSQL_HOST=db\0MYSQL_PASSWORD=p\0AGENTO_ENCRYPTION_KEY=secret\0CONFIG__A__B=c\0"
    )
    with patch.object(renderer, "ENV_FILE", str(env)):
        settings = renderer._mysql_settings()
    assert settings == {"MYSQL_HOST": "db", "MYSQL_PASSWORD": "p"}
    assert "AGENTO_ENCRYPTION_KEY" not in settings


# --- The emitted line ------------------------------------------------------------------

def test_the_installer_line_is_re_emitted_so_the_renderer_keeps_running():
    out = _render([])
    assert renderer.INSTALLER_LINE in out.splitlines()


def test_a_rendered_line_delivers_the_argv_the_renderer_built(tmp_path, modules):
    """Execute the line against a stub launcher and assert the argv it receives."""
    launcher = tmp_path / "launch.sh"
    out_file = tmp_path / "argv.txt"
    launcher.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        printf '%s\\n' "$@" > {out_file}
    """))
    launcher.chmod(0o755)

    log = tmp_path / "cron-stderr.log"
    with patch.object(renderer, "LAUNCHER", str(launcher)), \
         patch.object(renderer, "LOG_REDIRECT", f">/dev/null 2>>{log}"):
        rendered = _render([_module(modules, "alpha", [
            {"name": "a", "schedule": "0 * * * *", "command": "a:b --msg 'two words'"},
        ])])
    line = next(ln for ln in rendered.splitlines() if "cron:run" in ln)
    command = line.split(" ", 5)[5]  # strip the 5 cron fields
    subprocess.run(["/bin/sh", "-c", command], check=True)

    assert out_file.read_text().splitlines() == [
        "--store", "--", renderer.RUN_SH, "cron:run", "alpha", "a:b", "--msg", "two words",
    ]


def test_the_renderer_runs_under_the_literal_root_cron_environment(tmp_path):
    """The exact command root's crontab carries — an empty environment plus PYTHONPATH."""
    repo_src = str(Path(__file__).resolve().parents[3] / "src")
    env_file = tmp_path / "env"
    env_file.write_bytes(b"MYSQL_HOST=db\0")
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent(f"""\
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("ic", {str(RENDERER_PATH)!r})
        m = importlib.util.module_from_spec(spec); sys.modules["ic"] = m
        spec.loader.exec_module(m)
        m.ENV_FILE = {str(env_file)!r}
        print(m._mysql_settings()["MYSQL_HOST"])
    """))
    result = subprocess.run(
        ["env", "-i", f"PYTHONPATH={repo_src}",
         "PATH=/usr/local/bin:/usr/bin:/bin", sys.executable, str(probe)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "db"


# --- schedule validation: a line cron rejects takes the whole crontab with it ------------

@pytest.mark.parametrize("schedule", [
    "* * * * *", "*/5 * * * *", "0 2 * * *", "0,30 1-5 * * mon-fri", "15 0 1 jan *",
    "@hourly", "@midnight",
])
def test_a_valid_schedule_is_accepted(schedule):
    assert renderer._validate_schedule(schedule) == schedule


@pytest.mark.parametrize("schedule", [
    "nope nope nope nope nope",   # alphabetic where a number belongs
    "99 99 99 99 99",             # every field out of range
    "60 * * * *",                 # minute 60
    "* 24 * * *",                 # hour 24
    "* * 0 * *",                  # day-of-month 0
    "* * * 13 *",                 # month 13
    "* * * * 8",                  # day-of-week 8
    "*/0 * * * *",                # a zero step never fires
    "1-2-3 * * * *",              # not a range
    "* * * *",                    # too few fields
    "* * * * * *",                # too many
    "",
])
def test_an_invalid_schedule_is_rejected(schedule):
    with pytest.raises(renderer.ValidationError):
        renderer._validate_schedule(schedule)


# --- cron.json shape: valid JSON of the wrong type is one source, not the whole render ---

@pytest.mark.parametrize("document", [
    "[]",
    '{"jobs": null}',
    '{"jobs": [null]}',
    '{"jobs": [{"schedule": "* * * * *"}]}',           # no name
    '{"jobs": [{"name": "x", "schedule": 5}]}',        # schedule not a string
    '{"jobs": [{"name": "x", "schedule": "* * * * *", "command": ["a"]}]}',
])
def test_a_wrongly_typed_cron_json_raises_a_validation_error(tmp_path, document):
    path = tmp_path / "cron.json"
    path.write_text(document)

    with pytest.raises(renderer.ValidationError):
        renderer._read_cron_json(path)


def test_one_broken_module_manifest_does_not_stop_the_render(modules):
    good = _module(modules, "good", [{"name": "ok", "schedule": "0 * * * *", "command": "x:y"}])
    broken = _module(modules, "broken", '{"jobs": [null]}')

    rendered = _render([good, broken])

    assert "broken" not in rendered
    assert "good/ok" in rendered
    assert renderer.INSTALLER_LINE in rendered
