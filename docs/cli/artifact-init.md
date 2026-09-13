# artifact:init

Create a versioned artifact, optionally importing a directory as its first version.

```bash
uv run bin/agento artifact:init <artifact_code> [--source <dir>] [--title <text>] [--owner <who>] [--actor <who>]
```

| Flag | Meaning |
|---|---|
| `--source <dir>` | Host directory to import as version 1. Omit for an empty artifact |
| `--title <text>` | Human-readable title, stored in the `versioned_artifact` table and shown by `artifact:list` |
| `--owner <who>` | Who owns the artifact, stored and shown the same way |
| `--actor <who>` | Recorded in the audit row (default `admin`) |

The metadata row only *decorates* the store, which stays the authority on what exists: a
failed INSERT is logged and the artifact is still created.

Version 1 is also written into the published tree and `current` is pointed at it, so the
`preview_url` the artifact answers from its first moment is the version the store calls
current. That step is never fatal either — if the published tree cannot be written, the
artifact exists and the next `artifact:publish` rebuilds it.

## The tool beside it

`versioned_artifact_init` does the same thing for the agent, so an agent can run the whole
lifecycle — init → draft → version → publish → next draft — unattended. This command is the
operator's equivalent, and it keeps the one capability the tool deliberately lacks: importing a
host directory as version 1.

The tool takes **`artifact_code` and an optional `title`, and nothing else**. No `--source`,
because a path parameter would hand an arbitrary host path to the model — the objection that
kept creation off the tool layer in the first place, and the only half of it that still holds.
An agent fills version 1 the way it changes any other version: `create_draft`, write on the
desk, `save_version`. No `--owner` either: a free-text owner cannot authorize anything, because
a failed metadata INSERT leaves the artifact standing.

The tool may create `av<agent_view_id>-*` — derived from the session, never configured — plus
any code `allowed_artifacts` grants that scope, up to `limits/max_agent_artifacts`. This command
is exempt from both. See [the module guide](../modules/versioned-artifacts.md) for the scoping
rules and their known limit.

## How it runs

Unlike most commands, this one is **not** proxied into the cron container — cron can see
neither the host source directory nor the toolbox-only storage volume. It runs on the
host, walks `--source` itself, and pipes a JSON payload into the toolbox:

```
docker compose <flags> exec -T toolbox node \
  /app/modules/core/versioned_artifacts/toolbox/cli.js --op init --actor <who>
```

Before walking `--source` it makes one extra call, `… cli.js --print-limits`, and uses the
answer as its pre-flight budget. The host cannot resolve module config itself — the
ENV → DB → `config.json` fallback reads a database on the container network — so asking
the toolbox is what keeps `config:set versioned_artifacts/limits/...` effective here instead
of being overridden by a constant compiled into the command. If the toolbox does not
answer, the command stops rather than guessing a default.

The toolbox side is the *same* service the MCP tools use, so the administrative path
gets the same locking, limits and audit row as an agent-driven change.

Because the command is host-local but provided by a module, it is listed in
`_LOCAL_MODULE_COMMANDS` rather than `_LOCAL_COMMANDS`: the former stops the Docker
proxy, the latter would also skip the module bootstrap that registers the command with
argparse.

## Source handling

- A top-level `.git` directory is skipped.
- A symbolic link **aborts** the import rather than being silently dropped, so an
  administrator learns their source contained one.
- A source `.gitignore` is ignored: every file walked is imported.
- Files are read on the host and sent base64-encoded; the toolbox never opens an
  arbitrary path.

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a
traceback. Re-running after a failed import is safe: a failed creation leaves no artifact
behind.
