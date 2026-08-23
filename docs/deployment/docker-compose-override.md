# Customizing Docker Compose

Agento uses two Docker Compose files:

| File | Owner | Updated on upgrade? |
|------|-------|---------------------|
| `docker/docker-compose.yml` | Agento | **Yes** — overwritten by `agento upgrade` and `agento install --reinstall` |
| `docker/docker-compose.override.yml` | You | **No** — never touched by Agento |

Docker Compose automatically merges both files when you run `docker compose up`. Your overrides deeply merge into the base config — you can add volumes, environment variables, and new services.

## Adding a volume to an existing service

To mount a custom directory into the toolbox container:

```yaml
# docker/docker-compose.override.yml
services:
  toolbox:
    volumes:
      - ../my-data:/app/my-data:ro
```

The volume is **appended** to the volumes already defined in `docker-compose.yml` — existing mounts are preserved.

## Adding environment variables

```yaml
# docker/docker-compose.override.yml
services:
  cron:
    environment:
      - MY_CUSTOM_VAR=value
```

## Adding a new service

```yaml
# docker/docker-compose.override.yml
services:
  redis:
    image: redis:7-alpine
    networks:
      - agento-net
    restart: unless-stopped
```

Make sure to attach custom services to `agento-net` if they need to communicate with Agento containers.

## Extending a managed image (custom Dockerfile)

To layer extra dependencies on top of a managed image (toolbox, sandbox, or cron), point your override at a custom Dockerfile that uses the managed tag as its base:

```yaml
# docker/docker-compose.override.yml
services:
  toolbox:
    build:
      context: ..
      dockerfile: docker/toolbox-custom/Dockerfile
      args:
        AGENTO_VERSION: ${AGENTO_VERSION:-latest}
    image: my-toolbox-custom:latest
```

```dockerfile
# docker/toolbox-custom/Dockerfile
ARG AGENTO_VERSION=latest
FROM agento-toolbox:${AGENTO_VERSION}

RUN npm install --no-save webdriverio @playwright/test
RUN npx playwright install chromium
```

`agento install` and `agento upgrade` build the managed `agento-<service>:<version>` base images directly (via `docker build`) **before** running `docker compose build`, so the `FROM agento-toolbox:${AGENTO_VERSION}` reference always resolves. Without this pre-build step, an override that defines its own `build:` section would completely replace the managed build, leaving the base tag missing and the override build would fail with `pull access denied` on Docker Hub.

## Pinning agent CLI versions

The sandbox image installs agent CLIs from npm, one per enabled module's `sandbox_package` declaration (today: `@anthropic-ai/claude-code`, `@openai/codex` and `@earendil-works/pi-coding-agent`). Without a pin, every rebuild grabs `latest` — and upstream patch releases sometimes ship behavior changes (e.g. claude-code 2.1.69 silently disabled `.mcp.json` trust auto-approval). Agento soft-pins each agent's CLI from that agent's own module declaration in `di.json`:

```jsonc
// src/agento/modules/claude/di.json (shipped with the framework)
{
  "agent_harnesses": [
    {
      "id": "claude",
      "class": "src.adapter.ClaudeHarnessAdapter",
      "default_provider": "anthropic",
      "providers": [ /* ... */ ],
      "sandbox_package": {
        "manager": "npm",
        "package": "@anthropic-ai/claude-code",
        "binary": "claude",
        "version_env_key": "CLAUDE_CODE_VERSION",
        "default_range": "2.1.165"
      }
    }
  ]
}
```

At install/upgrade time, the framework enumerates every enabled module's `sandbox_packages` and writes one line per declaration to `docker/.env`:

```bash
# docker/.env (seeded by agento install — one line per declared agent)
CLAUDE_CODE_VERSION=~2.1.142
CODEX_VERSION=~0.128.0
```

The tilde range allows patch upgrades within the tested minor and forbids minor jumps. Each `agento` release ships a new tested default (in the module's `di.json`), but **`agento upgrade` never overwrites your existing `docker/.env` pin** — if it sees an older value, it logs a warning and moves on. Bump it intentionally when you're ready.

To test a new upstream release without waiting for an agento release:

```bash
# Edit docker/.env to set the new range
# Then rebuild the sandbox image and restart:
agento upgrade --no-restart
cd docker && docker compose build sandbox && docker compose up -d sandbox
```

`agento doctor` (run from inside the project) compares the live `<binary> --version` from the running sandbox against the pin for every enabled agent module and warns if they drift — usually meaning someone edited the pin but didn't rebuild.

### Adding a new agent

To register a new agent (e.g. OpenCode, Hermes), drop a module under `app/code/<name>/` with a `module.json` and a `di.json` declaring one `agent_harnesses` entry that carries a `sandbox_package` (see [harness-contract.md](../architecture/harness-contract.md)). On the next `agento upgrade`:

- `docker/.env` gains a `<KEY>=<default_range>` line for the new agent
- `docker-compose.yml` gains a `<KEY>: ${<KEY>:-<default>}` build arg under the sandbox service
- `agento doctor` adds a `<binary> pin` row
- The framework needs zero edits

*(This limitation is gone. The sandbox Dockerfile is rendered from the enabled modules' `sandbox_package` declarations, so a third agent's CLI is installed with no framework edit — `pi` was added exactly that way. Regenerate after enabling a module: `python -c 'from agento.framework.cli._provisioning import render_dev_sandbox_dockerfile as r; r()'`, or let `agento install`/`upgrade` do it.)*

## Combining multiple overrides

You can add volumes, env vars, and new services in the same file:

```yaml
# docker/docker-compose.override.yml
services:
  toolbox:
    volumes:
      - ../my-tools:/app/my-tools:ro

  cron:
    environment:
      - CUSTOM_FLAG=1

  mobile-emulator:
    image: my-emulator:latest
    ports:
      - "8080:8080"
    networks:
      - agento-net
    restart: unless-stopped
```

## How it works

Docker Compose merges files in this order:

1. `docker-compose.yml` — base (managed by Agento)
2. `docker-compose.override.yml` — your customizations (auto-detected by Docker)

For lists (volumes, environment, ports), values are **appended**. For scalars (image, restart), the override **replaces** the base value. See [Docker Compose documentation](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/) for full merge rules.

## Upgrading

`agento upgrade` overwrites `docker-compose.yml` but never touches `docker-compose.override.yml`. Your customizations survive every upgrade.
