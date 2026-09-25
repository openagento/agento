# Cron container privileges — what runs as root, and why

The cron container runs **one** uid for every agent_view (`agent`) on **one** shared
`/workspace`. That is deliberate: agent_views seeing each other's files is how a task is handed
from one view to another. What is *not* shared is the **credential store** — the database
credentials and `AGENTO_ENCRYPTION_KEY`, which together decrypt every stored credential of
every view.

Until 2026-09-23 the store was in a mode-0644 file that any agent could read
(`DECISIONS.md` D-SSH-1 residual channel 6). This document describes what replaced it.

> **This is not isolation between agent_views.** One uid, one `/workspace`: a view can still
> read a peer's artifacts. What is closed is the *store*, not the filesystem.

## The three properties

The design is judged against these, and nothing here may weaken one to simplify another:

- **P1** — no path readable or writable at uid `agent` yields the store.
- **P2** — no code uid `agent` can author or influence is executed by root.
- **P3** — uid `agent` cannot *choose* what gets the store. The capability is fixed root-side
  policy over data the agent cannot write, never a flag, a field or a path a producer supplies.

## What runs as root

Three image-owned programs, all in `root:root 0700` files inside a `root:root` directory
(write permission on the *directory* would let `agent` unlink and replace a root-owned file
whatever that file's own mode said):

| Program | Runs | Does |
|---------|------|------|
| `/entrypoint.sh` | at container start | splits the environment, installs root's crontab, starts the consumer and `setup:upgrade` through the launcher |
| `/opt/cron-agent/split-env.py` | once, from the entrypoint | partitions the environment into the store file and the public file |
| `/opt/cron-agent/install-crontab.py` | every minute, from root's crontab | renders the managed crontab |
| `/opt/cron-agent/launch.sh` | for every agent-uid process | clears the environment, re-adds the public set, drops privilege |

Everything else — module bootstrap, migrations, data patches, observers, every CLI command,
the consumer, every job — runs as `agent`. In particular `setup:upgrade` is **not** run as
root: it executes module data-patch classes, including from `app/code/` and PyPI extensions
(**P2**).

## The privilege drop

`/opt/cron-agent/launch.sh [--store] -- <argv…>`

```bash
/opt/cron-agent/launch.sh --store -- /opt/cron-agent/run.sh consumer
```

1. **Re-exec through `env -i`.** `setpriv` *preserves* the environment — that is precisely why
   it replaces `su - agent -c`, whose wipe was the only reason a world-readable env file had to
   exist — and root's own environment came from docker with the store in it. The clean-marker
   is an internal **positional argument**, never an environment variable: a variable can be
   inherited, and the cron service loads `../secrets.env`, so an operator could otherwise set it.
2. **Import `/opt/cron-agent/env.public`** — NUL-delimited, read with `export "$entry"`.
   It is never `source`d: `. file` evaluates a `$(…)` inside a value **as root**. A record whose
   name is not a shell identifier is skipped, not fatal — one malformed name must not abort
   every launch.
3. **`setpriv --reuid "$(id -u agent)" --regid "$(id -g agent)" --init-groups`** (numeric:
   the account's primary group is the image's `HOST_GID`, so no group is named `agent`), plus the login environment `su -`
   used to supply (`HOME`, `USER`, `PATH`). No `--reset-env`: the environment is already
   exactly what the launcher built.
4. **`--store` does not use `setpriv` at all.** It stays root and `exec`s
   `/opt/cron-agent/drop.py`, whose only permitted command is the framework CLI (`run.sh`).

`drop.py` reads the store while it is still root, drops **every** id to `agent` in-process
(`setgroups`/`setresgid`/`setresuid`), calls `prctl(PR_SET_DUMPABLE, 0)`, loads the payload into
`framework/store_env.py`'s **private module-level mapping**, and only then imports the framework.

Why in-process: `execve` resets `PR_SET_DUMPABLE` to 1, and these images carry no yama
`ptrace_scope`, so a store handed **across** an exec — on a descriptor, in argv, in the
environment — is reachable to a same-uid `PTRACE_ATTACH` for the whole of Python's startup.
There is no exec after the drop, so there is no window. The mapping is deliberately not
`os.environ`: that would not rewrite this process's `/proc/<pid>/environ` (fixed at `execve`)
but it *would* be passed to every child at its own `execve`, where it is peer-readable.
Delivery **fails closed** — an unreadable store file, or a malformed record, aborts.

A command that needs no store (a `test -f` probe, `logrotate`) takes the `setpriv` branch and
never becomes non-dumpable, so it must never hold secrets it has no use for.

## How the crontab is built

The managed crontab belongs to **root**, so an unwrapped line would run as root. Root therefore
constructs every line itself, from two inputs uid `agent` cannot write:

1. **Every *installed* module's `cron.json`**, discovered with
   `module_discovery.iter_module_dirs()` — core modules, `app/code` (which shadows) and PyPI
   extensions bind-mounted at `/opt/agento-src/<ext>`. Each file is read with `json.load`, as
   data: no module class is imported (**P2**).
2. **The `schedule` table**, which `jira:periodic:sync` keeps current. The agent has no database
   credential, so it cannot write the table except through the toolbox's own job flow.

**It is `iter_module_dirs()`, never `iter_enabled_module_dirs()`.** The latter reads
`app/etc/modules.json`, which lives on a writable mount and which `mo:en`/`mo:di` edit as
`agent`. Rendering from the *installed* catalog keeps root's line set independent of everything
the agent can write (**P3**).

**Enablement is enforced after the drop.** A module job is rendered as
`run.sh cron:run <module> <command…>`, never as the bare command. `cron:run` is an internal
framework command: it resolves the module's enabled state and, when the module is disabled,
exits **0** without importing it; otherwise it dispatches the inner command **in-process** (an
`exec` would discard the fd-delivered store and reset `PR_SET_DUMPABLE`). That is what lets
CLAUDE.md's "disabling a module must leave the system fully operational" and **P3** hold at once.

**The store requirement is fixed policy, not a declarable field (P3).** There is no
`needs_store` anywhere. The rule is one line in the renderer:

> a job whose executable is `/opt/cron-agent/run.sh` (the framework CLI) is launched with
> `--store`; every other executable — a `raw_command` such as `logrotate` — is launched without it.

A module cannot name an executable at all: `raw_command` is accepted **only** from the
framework's own `cron.json`, and rejected from a module by the renderer and by `module:validate`.
A module `raw_command` would escape the dispatcher, so a *disabled* module's raw job would keep
firing.

### Validation, and two failure policies

Every rendered line is argv the renderer built: `shlex.split` the declared command, then
`shlex.quote` each item. A schedule must match a strict 5-field cron expression or an
`@hourly`-style keyword. An argv item containing CR, LF, NUL — or **`%`** — is rejected rather
than escaped: cron turns `%` into a newline and feeds it to the command on stdin *before* any
shell quoting applies, so it cannot be escaped. Untrusted text (a jira `summary`) is stripped of
CR/LF and reaches only a `#` comment.

| Failure | Policy |
|---------|--------|
| **Validation** — malformed `cron.json`, bad schedule, rejected argv | omit only that module or that row; everything else still renders |
| **Operational** — env file unreadable, database unreachable, query error | leave the current crontab **byte-identical**; render nothing |

The second is the one that matters: under a single per-source rule a transient database outage
would silently delete every jira schedule.

## The host CLI's exec proxies

The cron service is handed the store by compose, so `docker compose exec -u agent cron …` would
inherit `MYSQL_*` directly. Every cron exec the CLI builds therefore enters as **root** and goes
through the launcher (`cli/_cron_exec.py`). The rule, enforced by a structural test: **no
`-u agent` in a compose exec whose service is `cron`**.

The two execs in `cli/run.py` that target the **`sandbox`** service keep `-u agent` and must not
be routed — the sandbox has no launcher and no store to withhold, and it is where the agent
itself runs. A test pins them, so a later over-eager sweep cannot take them.

No docker socket is mounted into cron, so uid `agent` cannot reach these paths at all. What they
close is a co-resident agent reading `/proc/<pid>/environ` of an operator-initiated command.

## Residual

Root itself. The store no longer crosses an `execve`, so the dumpable window an exec reopens
carries nothing: `drop.py` holds the payload only after it is both unprivileged and
non-dumpable.

Peer **artifact** reads remain open and accepted: one uid, one `/workspace`, by design.

See `DECISIONS.md` D-SSH-1 and [cron-env-contract.md](cron-env-contract.md).
