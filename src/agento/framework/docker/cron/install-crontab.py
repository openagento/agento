#!/usr/bin/env python3
"""Render root's crontab from sources uid ``agent`` cannot write.

Runs as **root**, every minute, from one fixed line in root's own crontab. It reads two
authoritative inputs — every *installed* module's ``cron.json`` and the ``schedule`` table —
and emits one launcher line per job. Neither input is agent-writable: ``app/etc/modules.json``
is deliberately NOT read here (it lives on a writable mount and ``mo:en``/``mo:di`` edit it as
``agent``), and enablement is enforced *after* the privilege drop by ``cron:run``.

The store requirement is fixed policy, not a declarable field: a job whose executable is the
framework CLI (``run.sh``) gets ``--store``; anything else does not.

Two failure policies. A **validation** failure (malformed ``cron.json``, bad schedule, rejected
argv) omits only the offending module or row. An **operational** failure (env file unreadable,
DB unreachable) leaves the current crontab byte-identical and renders nothing — otherwise a
transient outage would silently delete every schedule.

See docs/architecture/cron-privileges.md.
"""
from __future__ import annotations

import functools
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agento.framework import store_env
from agento.framework.module_discovery import iter_module_dirs, resolve_module_root
from agento.framework.module_validator import _PLACEHOLDER_RE

ENV_FILE = "/opt/cron-agent/env"
LAUNCHER = "/opt/cron-agent/launch.sh"
RUN_SH = "/opt/cron-agent/run.sh"
VENV_PYTHON = "/opt/cron-agent/.venv/bin/python"
INSTALLER = "/opt/cron-agent/install-crontab.py"
LOG_REDIRECT = ">/dev/null 2>>/app/logs/cron-stderr.log"

# The renderer rewrites root's whole crontab, so it must re-emit the line that invokes it.
# `cron` hands its child a minimal environment, so the interpreter and PYTHONPATH are named
# explicitly: the image installs agento via `uv sync --no-install-project` and reaches it
# through PYTHONPATH. Kept byte-identical to the entrypoint's bootstrap copy (guard test).
INSTALLER_LINE = (
    "* * * * * env -i PYTHONPATH=/opt/agento-src PATH=/usr/local/bin:/usr/bin:/bin "
    f"{VENV_PYTHON} {INSTALLER} {LOG_REDIRECT}"
)

# Per-field ranges, in crontab order. Checked field by field rather than with one regex:
# a regex loose enough to accept `*/5`, `1-5` and `mon` also accepts `nope` and `99`, and a
# line cron rejects takes the WHOLE crontab down with it — every other job included.
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = (
    {}, {}, {},
    {n: i + 1 for i, n in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])},
    {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])},
)
_CRON_KEYWORDS = frozenset({
    "@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually", "@midnight",
})
# `%` is consumed by cron itself (turned into a newline and fed to the command on stdin)
# before any shell quoting applies, so it can never be escaped — only rejected.
_FORBIDDEN_IN_ARGV = ("\r", "\n", "\0", "%")


class ValidationError(Exception):
    """Bad content in one source — omit that source, keep rendering the rest."""


class OperationalError(Exception):
    """A source is unavailable — change nothing at all."""


@dataclass(frozen=True)
class Job:
    comment: str
    schedule: str
    argv: tuple[str, ...]

    @property
    def wants_store(self) -> bool:
        return self.argv[0] == RUN_SH

    def to_line(self) -> str:
        store = "--store " if self.wants_store else ""
        quoted = " ".join(shlex.quote(a) for a in self.argv)
        return (
            f"# {self.comment}\n"
            f"{self.schedule} {LAUNCHER} {store}-- {quoted} {LOG_REDIRECT}"
        )


def _validate_number(text: str, index: int) -> int:
    low, high = _FIELD_RANGES[index]
    value = _FIELD_NAMES[index].get(text.lower())
    if value is None:
        if not text.isdigit():
            raise ValidationError(f"not a number: {text!r}")
        value = int(text)
    if not low <= value <= high:
        raise ValidationError(f"{value} is outside {low}-{high}")
    return value


def _validate_field(field: str, index: int) -> None:
    for part in field.split(","):
        item, sep, step = part.partition("/")
        if sep and (not step.isdigit() or int(step) == 0):
            raise ValidationError(f"invalid step: {part!r}")
        if item == "*":
            continue
        bounds = item.split("-")
        if len(bounds) > 2 or any(not b for b in bounds):
            raise ValidationError(f"invalid range: {part!r}")
        for bound in bounds:
            _validate_number(bound, index)


def _validate_schedule(schedule: str) -> str:
    schedule = (schedule or "").strip()
    if schedule in _CRON_KEYWORDS:
        return schedule
    fields = schedule.split()
    if len(fields) != 5:
        raise ValidationError(f"invalid cron schedule: {schedule!r}")
    for index, field in enumerate(fields):
        try:
            _validate_field(field, index)
        except ValidationError as exc:
            raise ValidationError(f"invalid cron schedule {schedule!r}: {exc}") from None
    return schedule


def _validate_argv(argv: list[str]) -> tuple[str, ...]:
    if not argv:
        raise ValidationError("empty command")
    for item in argv:
        for bad in _FORBIDDEN_IN_ARGV:
            if bad in item:
                raise ValidationError(f"forbidden character {bad!r} in argv item {item!r}")
    return tuple(argv)


def _comment(text: str) -> str:
    """Untrusted text (a jira summary) reaches a ``#`` comment and nothing else."""
    return text.replace("\r", " ").replace("\n", " ")


def _read_cron_json(path: Path) -> list[dict]:
    """Every shape is checked here: valid JSON of the wrong type is the likelier defect,
    and an uncaught ``AttributeError`` would abort the whole render, not this one source."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: top level is not an object")
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValidationError(f"{path}: 'jobs' is not a list")
    for entry in jobs:
        if not isinstance(entry, dict):
            raise ValidationError(f"{path}: a job entry is not an object")
        for field in ("name", "schedule"):
            if not isinstance(entry.get(field), str):
                raise ValidationError(f"{path}: job {field!r} is missing or not a string")
        for field in ("command", "raw_command"):
            if field in entry and not isinstance(entry[field], str):
                raise ValidationError(f"{path}: job {field!r} is not a string")
    return jobs


def _framework_jobs() -> list[Job]:
    """The framework's own ``cron.json`` — the only source allowed a ``raw_command``."""
    import agento.framework

    manifest = Path(agento.framework.__file__).parent / "cron.json"
    if not manifest.is_file():
        return []
    jobs: list[Job] = []
    for entry in _read_cron_json(manifest):
        raw = entry.get("raw_command", "")
        command = entry.get("command", "")
        argv = shlex.split(raw) if raw else [RUN_SH, *shlex.split(command)]
        jobs.append(Job(
            comment=_comment(entry["name"]),
            schedule=_validate_schedule(_resolve_schedule(entry["schedule"])),
            argv=_validate_argv(argv),
        ))
    return jobs


def _module_jobs(module_dir: Path) -> list[Job]:
    """One installed module's jobs, each wrapped in the ``cron:run`` dispatcher.

    A module ``raw_command`` is rejected: it would escape the dispatcher, so a *disabled*
    module's raw job would keep firing. ``module:validate`` rejects it too.
    """
    manifest = module_dir / "cron.json"
    if not manifest.is_file():
        return []
    jobs: list[Job] = []
    for entry in _read_cron_json(manifest):
        if entry.get("raw_command"):
            raise ValidationError(
                f"{manifest}: raw_command is not allowed in a module cron.json"
            )
        command = entry.get("command", "")
        if not command.strip():
            raise ValidationError(f"{manifest}: empty command")
        argv = [RUN_SH, "cron:run", module_dir.name, *shlex.split(command)]
        jobs.append(Job(
            comment=_comment(f"{module_dir.name}/{entry['name']}"),
            schedule=_validate_schedule(_resolve_schedule(entry["schedule"])),
            argv=_validate_argv(argv),
        ))
    return jobs


def _mysql_settings() -> dict[str, str]:
    """MySQL fields only — the renderer decrypts nothing and never reads the crypto key."""
    try:
        raw = Path(ENV_FILE).read_bytes()
    except OSError as exc:
        raise OperationalError(f"cannot read {ENV_FILE}: {exc}") from exc
    try:
        parsed = store_env.parse(raw)
    except ValueError as exc:
        raise OperationalError(f"cannot parse {ENV_FILE}: {exc}") from exc
    return {k: v for k, v in parsed.items() if k.startswith("MYSQL_")}


def _connect():
    import pymysql

    env = _mysql_settings()
    try:
        return pymysql.connect(
            host=env.get("MYSQL_HOST", "mysql"),
            port=int(env.get("MYSQL_PORT", "3306")),
            user=env.get("MYSQL_USER", "cron_agent"),
            password=env.get("MYSQL_PASSWORD", ""),
            database=env.get("MYSQL_DATABASE", "cron_agent"),
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        raise OperationalError(f"database unavailable: {exc}") from exc


@functools.cache
def _config_overrides() -> tuple[dict[str, str], dict[str, tuple[str, bool]]]:
    """``CONFIG__*`` from the env file and default-scope ``core_config_data`` rows.

    Loaded once, and only when a schedule holds a directive. Nothing is decrypted.
    """
    try:
        env = store_env.parse(Path(ENV_FILE).read_bytes())
    except (OSError, ValueError) as exc:
        raise OperationalError(f"cannot read {ENV_FILE}: {exc}") from exc
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT path, value, encrypted FROM core_config_data "
                "WHERE scope = 'default' AND scope_id = 0"
            )
            db = {r["path"]: (r["value"], bool(r["encrypted"])) for r in cur.fetchall()}
    except Exception as exc:
        raise OperationalError(f"config query failed: {exc}") from exc
    finally:
        conn.close()
    return {k: v for k, v in env.items() if k.startswith("CONFIG__")}, db


def _config_value(path: str) -> str | None:
    """One ``{module/path}`` value: ENV -> DB -> the module's ``config.json``."""
    env, db = _config_overrides()
    env_key = "CONFIG__" + path.upper().replace("-", "_").replace("/", "__")
    if env_key in env:
        return env[env_key]
    row = db.get(path.replace("-", "_"))
    if row is not None:
        value, encrypted = row
        if encrypted:
            raise ValidationError(f"config path '{path}' is encrypted; a schedule must be plain")
        return value
    module, _, field = path.partition("/")
    config_json = next(
        (d / "config.json" for d in iter_module_dirs(resolve_module_root()) if d.name == module),
        None,
    )
    if config_json is not None and config_json.is_file():
        value = json.loads(config_json.read_text()).get(field)
        return None if value is None else str(value)
    return None


def _resolve_schedule(schedule: str) -> str:
    """Replace ``{module/path}`` directives with config values; a literal is unchanged."""
    def _sub(match) -> str:
        value = _config_value(match.group(1))
        if not value:
            raise ValidationError(
                f"cron schedule references config path '{match.group(1)}', which is not set"
            )
        return value

    return _PLACEHOLDER_RE.sub(_sub, schedule)


def _schedule_jobs() -> list[Job]:
    """Recurring jira rows, scoped exactly like ``jira:periodic:sync`` writes them."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT av.id FROM agent_view av
                JOIN workspace w ON w.id = av.workspace_id
                WHERE av.is_active = 1 AND w.is_active = 1
                ORDER BY av.id
                """
            )
            active_ids = [row["id"] for row in cur.fetchall()]
            if active_ids:
                placeholders = ",".join(["%s"] * len(active_ids))
                cur.execute(
                    f"""
                    SELECT s.issue_key, s.summary, s.cron_expr, av.code AS agent_view_code
                    FROM schedule s
                    JOIN agent_view av ON av.id = s.agent_view_id
                    WHERE s.agent_type = 'cron' AND s.enabled = TRUE
                      AND s.agent_view_id IN ({placeholders})
                    ORDER BY s.agent_view_id, s.issue_key
                    """,
                    active_ids,
                )
            else:
                cur.execute(
                    """
                    SELECT s.issue_key, s.summary, s.cron_expr, '' AS agent_view_code
                    FROM schedule s
                    WHERE s.agent_type = 'cron' AND s.enabled = TRUE
                      AND s.agent_view_id IS NULL
                    ORDER BY s.issue_key
                    """
                )
            rows = cur.fetchall()
    except Exception as exc:
        raise OperationalError(f"schedule query failed: {exc}") from exc
    finally:
        conn.close()

    jobs: list[Job] = []
    for row in rows:
        view_code = row["agent_view_code"] or ""
        command = ["publish", "jira-cron", row["issue_key"]]
        if view_code:
            command += ["--agent-view", view_code]
        try:
            jobs.append(Job(
                comment=_comment(f"{row['issue_key']}: {row['summary'] or ''}"),
                schedule=_validate_schedule(row["cron_expr"] or ""),
                argv=_validate_argv([RUN_SH, "cron:run", "jira_periodic_tasks", *command]),
            ))
        except ValidationError as exc:
            print(f"install-crontab: skipping row {row['issue_key']}: {exc}", file=sys.stderr)
    return jobs


def collect_jobs() -> list[Job]:
    """Every renderable job. Validation failures drop one source; they never abort."""
    jobs: list[Job] = []
    for source in (_framework_jobs,):
        try:
            jobs += source()
        except (ValidationError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            print(f"install-crontab: skipping framework cron.json: {exc}", file=sys.stderr)

    for module_dir in iter_module_dirs(resolve_module_root()):
        try:
            jobs += _module_jobs(module_dir)
        except (ValidationError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            print(f"install-crontab: skipping {module_dir.name}: {exc}", file=sys.stderr)

    jobs += _schedule_jobs()  # OperationalError propagates — see render()
    return jobs


def render() -> str:
    jobs = collect_jobs()
    lines = [
        "# Generated by /opt/cron-agent/install-crontab.py. Do not edit manually.",
        "SHELL=/bin/bash",
        "",
        INSTALLER_LINE,
        "",
    ]
    lines += [job.to_line() for job in jobs]
    return "\n".join(lines) + "\n"


def current_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def main() -> int:
    try:
        new = render()
    except OperationalError as exc:
        # A source is unavailable: keep the crontab exactly as it is.
        print(f"install-crontab: unchanged ({exc})", file=sys.stderr)
        return 0
    if new == current_crontab():
        return 0
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
