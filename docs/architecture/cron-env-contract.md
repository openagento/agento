# Cron Container Env-Var Contract

Any env var the cron/consumer needs from `docker-compose` (or
`docker-compose.override.yml`) must match the cron container's prefix whitelist —
otherwise it is silently dropped before the consumer reads it.

## Why a whitelist exists

The cron container's entrypoint **partitions** docker-injected env into two files, by
`credential_store_env.is_credential_store_name` — the same predicate that strips the store
from an agent spawn:

| File | Holds | Mode | How it reaches a process |
|------|-------|------|--------------------------|
| `/opt/cron-agent/env` | `MYSQL_*`, `CONFIG__*`, `AGENTO_ENCRYPTION_KEY` — **the credential store** | `root:root 0600` | read by root-owned `drop.py`, which drops privilege in-process and loads it into a private mapping (`framework/store_env.py`). Never in any environment, and never across an `execve`. |
| `/opt/cron-agent/env.public` | everything else in the whitelist (`AGENTO_*`, `TZ`, `PYTHONPATH`, `PROVIDER`, `DISABLE_*`) | `0644` | exported by the launcher into the dropped process's environment |

Both files are written **NUL-delimited** (`env -0`), so a value containing a newline, a
quote or shell syntax survives byte-identical — and neither is ever `source`d.

The privilege drop is `/opt/cron-agent/launch.sh` (`root:root 0700`):

```bash
/opt/cron-agent/launch.sh --store -- /opt/cron-agent/run.sh consumer
```

It re-execs itself through `env -i`, imports `env.public` with `export "$entry"` (no
evaluation), and drops privilege with `setpriv --reuid <uid> --regid <gid> --init-groups`, resolving both ids
with `id -u agent` / `id -g agent` (the account's primary group is the image's `HOST_GID`, and
no group is named `agent`).

Two facts make the whitelist load-bearing:

1. The launcher starts from an **empty** environment. `setpriv` *preserves* what it
   inherits — unlike the `su - agent` it replaced, which wiped it — so anything not written
   to `env.public` is gone by the time the consumer starts.
2. The split is what keeps a secret out of an agent-reachable environment. It is a security
   boundary as well as a parsing one: a name added later that matches a store shape lands on
   the `0600` side automatically, and a regression guard asserts `env.public` matches none of
   them.

See [cron-privileges.md](cron-privileges.md) for the whole root/agent split.

## Allowed prefixes

| Prefix / exact name | What it's for | Renameable? |
|---------------------|---------------|-------------|
| `AGENTO_*`          | **Framework knobs.** Use this prefix for any new env var the consumer/cron needs. | n/a — this is the canonical extensibility prefix |
| `MYSQL_*`           | Database driver config (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_PORT`) | No — driver convention |
| `CONFIG__*`         | Public 3-level config-fallback contract (ENV → DB → `config.json`) | No — public contract |
| `TZ`                | libc / cron daemon timezone | No — libc convention |
| `PYTHONPATH`        | Python module resolution | No — Python convention |
| `PROVIDER`          | Default agent provider for `agento run` | Could be renamed; not broken today |
| `DISABLE_LLM`       | Test/dev dry-run flag | Could be renamed; not broken today |
| `DISABLE_AUTOUPDATER` | Disables claude-code's built-in self-updater so the image's pinned version isn't silently superseded at runtime. Set to `1` via the sandbox Dockerfile `ENV`. | No — external (claude-code) convention |

Anything not matching one of the above is dropped before the consumer sees it.

**One value the whitelist does not carry, by design.** A `CONFIG__*` value lands in the
`0600` store file, which is a file on disk. For the SSH private key that is refused outright:
the entrypoints exit 78 when
`CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY` (or an ambient `AGENTO_SSH_PRIVATE_KEY`,
`AGENTO_SSH_TTL`, `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GIT_SSH_COMMAND`) is set in the container
environment — writing the key into that file would put it on disk, which is exactly what
[D-SSH-1](../../DECISIONS.md) removed. Set it in the DB. Other multiline fields (instructions, the
non-secret SSH files) do survive the NUL-delimited files intact; the DB is still the right place.

## The rule for new framework / module knobs

> **Use `AGENTO_*` as the prefix.**

Examples:

- `AGENTO_CONSUMER_MAX_WORKERS`
- `AGENTO_CONSUMER_POLL_INTERVAL`
- `AGENTO_JOB_TIMEOUT_SECONDS`
- `AGENTO_WORKSPACE_DIR`

If a new var follows an external convention (e.g. a third-party SDK looks for
`OPENAI_API_KEY`), don't fight the convention — instead add the prefix to the
whitelist in `docker/cron/split-env.py` explicitly and document it in the table above.

## Verifying a var actually reaches the consumer

After setting an env var in `docker-compose.override.yml`:

```bash
cd docker
docker compose -f docker-compose.dev.yml restart cron
docker compose -f docker-compose.dev.yml exec -u root cron \
    tr '\0' '\n' < /opt/cron-agent/env.public | grep ^AGENTO_
```

The var must appear in the output. If it doesn't, the whitelist dropped it.

Cross-check the consumer's actual runtime values:

```bash
grep "Consumer starting" logs/consumer.log | tail -1
```

## Regression guard

`tests/unit/framework/test_entrypoint_env_whitelist.py` reads the whitelist out of
`docker/cron/split-env.py` and walks every `from_env()` classmethod under
`src/agento/framework/` (via AST), collecting each literal var name passed to
`os.environ.get(...)` or `store_env.get(...)`. Any var on neither side of the split fails CI
with a clear message. `tests/unit/framework/test_cron_container_privileges.py` asserts the
partition itself. Run them directly:

```bash
uv run pytest tests/unit/framework/test_entrypoint_env_whitelist.py \
              tests/unit/framework/test_cron_container_privileges.py -v
```

## Migration from pre-0.9.3 names

These vars were renamed because the entrypoint dropped them (no operator was
relying on the old names — they never worked in production):

| Old name                          | New name                                |
|-----------------------------------|-----------------------------------------|
| `CONSUMER_MAX_WORKERS`            | `AGENTO_CONSUMER_MAX_WORKERS`           |
| `CONSUMER_POLL_INTERVAL`          | `AGENTO_CONSUMER_POLL_INTERVAL`         |
| `JOB_TIMEOUT_SECONDS`             | `AGENTO_JOB_TIMEOUT_SECONDS`            |
| `AGENT_USAGE_WINDOW_HOURS`        | `AGENTO_AGENT_USAGE_WINDOW_HOURS`       |
| `AGENT_ROTATION_INTERVAL_HOURS`   | `AGENTO_AGENT_ROTATION_INTERVAL_HOURS`  |

If your `docker-compose.override.yml` sets any of the old names, rename them
on upgrade. The old names have no aliasing — they are silently ignored.

See [DECISIONS.md](../../DECISIONS.md) → "2026-05-14 — `AGENTO_*` prefix for cron container env vars".
