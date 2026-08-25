# Cron Container Env-Var Contract

Any env var the cron/consumer needs from `docker-compose` (or
`docker-compose.override.yml`) must match the entrypoint's prefix whitelist —
otherwise it is silently dropped before the consumer reads it.

## Why a whitelist exists

The cron container's entrypoint persists docker-injected env into
`/opt/cron-agent/env`:

```bash
env | grep -E '^(MYSQL_|TZ=|DISABLE_LLM=|DISABLE_AUTOUPDATER=|PROVIDER=|CONFIG__|AGENTO_|PYTHONPATH=)' > "$ENV_FILE"
```

It then starts the consumer under the unprivileged `agent` user:

```bash
su - agent -c "set -a; source $ENV_FILE; set +a; ... consumer"
```

Two facts make the whitelist load-bearing:

1. `su -` (with the leading dash) wipes the parent environment. Anything not
   written to `$ENV_FILE` is gone by the time the consumer starts.
2. The file is consumed via `source` — which means values containing quotes,
   newlines, or shell metacharacters must not enter it. The whitelist keeps the
   file safe to source.

So the whitelist is not a security boundary — it's a *parsing* boundary.
"Just pass everything" is not an option without rewriting the load step.

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

## The rule for new framework / module knobs

> **Use `AGENTO_*` as the prefix.**

Examples:

- `AGENTO_CONSUMER_MAX_WORKERS`
- `AGENTO_CONSUMER_POLL_INTERVAL`
- `AGENTO_JOB_TIMEOUT_SECONDS`
- `AGENTO_WORKSPACE_DIR`

If a new var follows an external convention (e.g. a third-party SDK looks for
`OPENAI_API_KEY`), don't fight the convention — instead add the prefix to the
entrypoint whitelist explicitly and document it in the table above.

## No env var for toolbox authentication — by design

Toolbox east-west auth adds **no** `AGENTO_*` variable, and that is the decision, not an oversight.

A shared signing key in the cron container would sit next to a shell-capable agent, so it is
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
docker compose -f docker-compose.dev.yml exec cron cat /opt/cron-agent/env | grep ^AGENTO_
```

The var must appear in the output. If it doesn't, the whitelist dropped it.

Cross-check the consumer's actual runtime values:

```bash
grep "Consumer starting" logs/consumer.log | tail -1
```

## Regression guard

`tests/unit/framework/test_entrypoint_env_whitelist.py` reads the regex out of
`entrypoint.sh` and walks every `from_env()` classmethod under
`src/agento/framework/` (via AST), collecting each literal var name passed to
`os.environ.get(...)`. Any var that doesn't match the whitelist fails CI
with a clear message. Run it directly:

```bash
uv run pytest tests/unit/framework/test_entrypoint_env_whitelist.py -v
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
