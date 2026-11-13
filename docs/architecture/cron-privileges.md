# Cron container privileges — what runs as root, and why

The cron container (the worker) runs the consumer, the crontab and every framework CLI
command. It holds the database credentials and `AGENTO_ENCRYPTION_KEY`. **No agent runs in
it**: every vendor CLI starts in a `runner-<i>` container, which has neither
([runner.md](runner.md)). `runner/test_spawn_guard.py` fails a new spawn in the worker.

So the worker hides nothing from a same-uid peer: it has no untrusted peer. Operator code in
`app/code` (cron jobs, observers, data patches) runs here with the database and the key. It is
trusted code (DECISIONS.md, 2026-10-09 WS6).

## The properties

- **P2** — no code uid `agent` can author or influence is executed by root.
- **P3** — what root runs is fixed root-side policy over data uid `agent` cannot write.

## What runs as root

Image-owned programs, in `root:root` files inside a `root:root` directory (write permission
on the *directory* would let `agent` unlink and replace a root-owned file whatever that file's
own mode said):

| Program | Runs | Does |
|---------|------|------|
| `/entrypoint.sh` | at container start | writes the env file, installs root's crontab, starts `setup:upgrade` and the consumer through the launcher |
| `/opt/cron-agent/install-crontab.py` (0700) | every minute, from root's crontab | renders the managed crontab |
| `/opt/cron-agent/launch.sh` (0700) | for every agent-uid process | clears the environment, imports the env file, drops privilege |

Everything else — module bootstrap, migrations, data patches, observers, every CLI command,
the consumer, every job — runs as `agent`. `setup:upgrade` is **not** run as root: it executes
module data-patch classes, including from `app/code/` and PyPI extensions (**P2**).

## The privilege drop

```bash
/opt/cron-agent/launch.sh -- /opt/cron-agent/run.sh consumer
```

1. **Re-exec through `env -i`.** `setpriv` *preserves* the environment, and root's own
   environment came from docker. The clean-marker is an internal **positional argument**, never
   an environment variable: a variable can be inherited.
2. **Import `/opt/cron-agent/env`** (`root:root 0600`, NUL-delimited, the whitelist of
   [cron-env-contract.md](cron-env-contract.md)) with `export "$entry"`. It is never `source`d:
   `. file` evaluates a `$(…)` inside a value **as root**. A record whose name is not a shell
   identifier is skipped, not fatal.
3. **`setpriv --reuid "$(id -u agent)" --regid "$(id -g agent)" --init-groups`** (numeric: the
   account's primary group is the image's `HOST_GID`), plus `HOME` and `USER`. No
   `--reset-env`: the environment is already what the launcher built.

## How the crontab is built

The managed crontab belongs to **root**, so an unwrapped line would run as root. Root therefore
constructs every line itself, from one input uid `agent` cannot write: **every *installed*
module's `cron.json`**, discovered with `module_discovery.iter_module_dirs()` — core modules,
`app/code` (which shadows) and PyPI extensions bind-mounted at `/opt/agento-src/<ext>`. Each file
is read with `json.load`, as data: no module class is imported (**P2**). A `{module/path}`
schedule directive reads that config value (ENV, then default-scope DB row, then `config.json`;
nothing is decrypted).

**The `schedule` table is not an input.** Its rows (Jira periodic tasks, user schedules) are
fired by the framework's `scheduler:tick`, a fixed line of the framework `cron.json` that runs
every minute as `agent` through the launcher ([schedules.md](schedules.md)). No row becomes a root crontab
line, so nothing a schedule row holds — an issue key, a title — reaches root. A guard test
(`test_the_renderer_never_reads_the_schedule_table`) fails when the renderer queries the table
again.

**It is `iter_module_dirs()`, never `iter_enabled_module_dirs()`.** The latter reads
`app/etc/modules.json`, which lives on a writable mount and which `mo:en`/`mo:di` edit as
`agent`. Rendering from the *installed* catalog keeps root's line set independent of everything
the agent can write (**P3**).

**Enablement is enforced after the drop.** A module job is rendered as
`run.sh cron:run <module> <command…>`, never as the bare command. `cron:run` is an internal
framework command: it resolves the module's enabled state and, when the module is disabled,
exits **0** without importing it; otherwise it dispatches the inner command **in-process**. That is what lets
CLAUDE.md's "disabling a module must leave the system fully operational" and **P3** hold at once.

A module cannot name an executable at all: `raw_command` is accepted **only** from the
framework's own `cron.json`, and rejected from a module by the renderer and by `module:validate`.
A module `raw_command` would escape the dispatcher, so a *disabled* module's raw job would keep
firing.

### Validation, and two failure policies

Every rendered line is argv the renderer built: `shlex.split` the declared command, then
`shlex.quote` each item. A schedule must match a strict 5-field cron expression or an
`@hourly`-style keyword. An argv item containing CR, LF, NUL — or **`%`** — is rejected rather
than escaped: cron turns `%` into a newline and feeds it to the command on stdin *before* any
shell quoting applies, so it cannot be escaped. A job name is stripped of CR/LF and reaches only
a `#` comment.

| Failure | Policy |
|---------|--------|
| **Validation** — malformed `cron.json`, bad schedule, rejected argv | omit only that module; everything else still renders |
| **Operational** — while a `{module/path}` directive is resolved: env file unreadable, database unreachable, query error | leave the current crontab **byte-identical**; render nothing |

The second is the one that matters: under a single per-source rule a transient database outage
would silently delete every job whose schedule is a directive. Without a directive the renderer
opens no database connection.

## The host CLI's exec proxies

`docker compose exec -u agent cron …` would hand the exec's own environment to the command.
Every cron exec the CLI builds therefore enters as **root** and goes through the launcher
(`cli/_cron_exec.py`). A structural test pins it: **no `-u agent` in a compose exec whose
service is `cron`**. The two execs in `cli/run.py` that target **`runner-1`** (`EXEC_SERVICE`)
keep `-u agent`.

## Residual

Root itself, and the operator's `app/code`. Peer **artifact** reads between agent_views remain
open and accepted: one uid, one `/workspace`, by design (in the runner now).

See `DECISIONS.md` D-SSH-1, [cron-env-contract.md](cron-env-contract.md) and
[zero-trust.md](zero-trust.md).
