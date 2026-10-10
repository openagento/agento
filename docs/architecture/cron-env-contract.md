# Cron Container Env-Var Contract

Any env var the cron/consumer needs from `docker-compose` (or
`docker-compose.override.yml`) must match the cron container's prefix whitelist —
otherwise it is silently dropped before the consumer reads it.

## Why a whitelist exists

The cron container's entrypoint writes the whitelisted part of the docker-injected env to
**one** file, `/opt/cron-agent/env` (`root:root 0600`, NUL-delimited, from `env -0`), with the
`PERSIST` pattern in `docker/cron/entrypoint.sh`. A value with a newline, a quote or shell
syntax survives byte-identical, and the file is never `source`d.

The privilege drop is `/opt/cron-agent/launch.sh` (`root:root 0700`):

```bash
/opt/cron-agent/launch.sh -- /opt/cron-agent/run.sh consumer
```

It re-execs itself through `env -i`, imports the file with `export "$entry"` (no
evaluation), and drops privilege with `setpriv --reuid <uid> --regid <gid> --init-groups`,
resolving both ids with `id -u agent` / `id -g agent` (the account's primary group is the
image's `HOST_GID`, and no group is named `agent`). `setpriv` *preserves* what it inherits,
so a name not in the file is gone by the time the consumer starts.

The file holds `MYSQL_*`, `CONFIG__*` and `AGENTO_ENCRYPTION_KEY`, and the consumer has them in
its environment. That is safe because no agent runs in this container: the runner runs every
vendor CLI and has no store ([runner.md](runner.md)). Until 0.17 the entrypoint split the store
into a second file that reached the CLI only through `drop.py`; WS6 removed that same-uid
stack (DECISIONS.md, 2026-10-09).

See [cron-privileges.md](cron-privileges.md) for the whole root/agent split.

## Allowed prefixes

| Prefix / exact name | What it's for | Renameable? |
|---------------------|---------------|-------------|
| `AGENTO_*`          | **Framework knobs.** Use this prefix for any new env var the consumer/cron needs. | n/a — this is the canonical extensibility prefix |
| `MYSQL_*`           | Database driver config (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_PORT`); `setup:upgrade` also reads `MYSQL_MIGRATE_PASSWORD` and `MYSQL_TOOLBOX_PASSWORD` ([upgrade.md](../cli/upgrade.md#database-users)) | No — driver convention |
| `CONFIG__*`         | Public 3-level config-fallback contract (ENV → DB → `config.json`) | No — public contract |
| `TZ`                | libc / cron daemon timezone | No — libc convention |
| `PYTHONPATH`        | Python module resolution | No — Python convention |
| `PROVIDER`          | Default agent provider for `agento run` | Could be renamed; not broken today |
| `DISABLE_LLM`       | Test/dev dry-run flag | Could be renamed; not broken today |
| `DISABLE_AUTOUPDATER` | Disables claude-code's built-in self-updater so the image's pinned version isn't silently superseded at runtime. Set to `1` via the sandbox Dockerfile `ENV`. | No — external (claude-code) convention |

Anything not matching one of the above is dropped before the consumer sees it.

**One value the whitelist does not carry, by design.** A `CONFIG__*` value lands in the
`0600` env file, which is a file on disk. For the SSH private key that is refused outright:
the entrypoints exit 78 when
`CONFIG__AGENT_VIEW__IDENTITY__SSH_PRIVATE_KEY` (or an ambient `AGENTO_SSH_PRIVATE_KEY`,
`AGENTO_SSH_TTL`, `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GIT_SSH_COMMAND`) is set in the container
environment — writing the key into that file would put it on disk, which is exactly what
[D-SSH-1](../../DECISIONS.md) removed. Set it in the DB. Other multiline fields (instructions, the
non-secret SSH files) do survive the NUL-delimited file intact; the DB is still the right place.

## The rule for new framework / module knobs

> **Use `AGENTO_*` as the prefix.**

Examples:

- `AGENTO_CONSUMER_MAX_WORKERS`
- `AGENTO_CONSUMER_POLL_INTERVAL`
- `AGENTO_JOB_TIMEOUT_SECONDS`
- `AGENTO_WORKSPACE_DIR`
- `AGENTO_RUNNER_COUNT` — how many `runner-<i>` services the compose renders. The
  rendered cron service gets the same number as a literal; set it in `docker/.env` and
  re-render (`agento upgrade` or `module:enable`). Lower it only after a drain
  ([runner.md](runner.md#the-owner-rule)).
- `AGENTO_RUNNER_SOCKET_DIR` — where the worker finds `runner-<i>/runner-<i>.sock`
  (default `/run/agento-runner`).

The runner services read `AGENTO_RUNNER_SOCKET`, `AGENTO_RUNNER_MAX_PROCS`,
`AGENTO_RUNNER_MAX_CONTROL` and `AGENTO_RUNNER_ACCEPT_RATE` from their own `environment:`
in compose. They do not go through this whitelist: a runner writes no env file.

If a new var follows an external convention (e.g. a third-party SDK looks for
`OPENAI_API_KEY`), don't fight the convention — instead add the prefix to the
`PERSIST` pattern in `docker/cron/entrypoint.sh` explicitly and document it in the table above.

## No env var for toolbox authentication — by design

Toolbox east-west auth adds **no** `AGENTO_*` variable, and that is the decision, not an oversight.

A shared signing key would have to reach every runner, next to a shell-capable agent, so it is
exfiltratable; and one key mints every scope, so a single leak grants the whole deployment. Capability
tokens live in the `toolbox_capability` table instead: each one is random, opaque, bound to one
agent_view (and one job), expiring, and revocable. Nothing reusable ever reaches the agent-adjacent
container.

So if you are adding a secret to this whitelist to let the cron talk to the toolbox — don't. Mint a
capability instead (`agento capability:mint`, or `issue_capability()` from framework code) and pass it
as `Authorization: Bearer`. See [docs/cli/capability.md](../cli/capability.md).

## Verifying a var actually reaches the consumer

After setting an env var in `docker-compose.override.yml`:

```bash
cd docker
docker compose -f docker-compose.dev.yml restart cron
docker compose -f docker-compose.dev.yml exec -u root cron \
    sh -c "tr '\\0' '\\n' < /opt/cron-agent/env" | grep ^AGENTO_
```

The var must appear in the output. If it doesn't, the whitelist dropped it.

Cross-check the consumer's actual runtime values:

```bash
grep "Consumer starting" logs/consumer.log | tail -1
```

## Regression guard

`tests/unit/framework/test_entrypoint_env_whitelist.py` reads the `PERSIST` pattern out of
`docker/cron/entrypoint.sh` and walks every `from_env()` classmethod under
`src/agento/framework/` (via AST), collecting each literal var name passed to
`os.environ.get(...)`. Any var the pattern does not match fails CI with a clear message.
`tests/unit/framework/test_cron_container_privileges.py` asserts the file is root-only. Run them directly:

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
