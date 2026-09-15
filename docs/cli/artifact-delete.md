# artifact:delete

Remove an artifact from every place it lives: the served pages, the store, and the
`versioned_artifact` row.

```bash
uv run bin/agento artifact:delete <artifact_code> [--actor <who>]
```

| Flag | Meaning |
|---|---|
| `--actor <who>` | Recorded in the audit row (default `admin`) |

**There is no tool equivalent, and there will not be one.** Every other lifecycle step —
init, draft, version, publish — is the agent's. This one is not: `agent_view_id` is
asserted by the caller, so ownership *scopes* and does not authorize, and a self-asserted
identity must not be able to unmake an immutable history. The toolbox refuses the
operation for any caller that is not the admin CLI.

The command asks before it acts, with an arrow-key selector whose **first** option keeps
the artifact — a mistaken Return destroys nothing.

## What it does, in order

1. The published tree `published/<code>/`. First, because it is the only root anyone can
   read over HTTP: a removal that dies halfway has stopped serving rather than left a
   live preview of an artifact the store no longer holds.
2. The store `<storage_root>/<code>/` — the bare repo, every open draft worktree, the
   locks, and the `owner` marker with them.
3. The `versioned_artifact` row. Like the `INSERT` in `init`, a failure here costs a
   stale title, never the removal — the store is the authority on what exists.

An audit row is written for the delete itself, and the artifact's earlier audit rows stay:
they record what happened, and what happened does not stop having happened.

## Repairing a half-removed artifact

Existence is checked on **both** roots, not through the store alone. So a store that lost
one root and kept the other — an interrupted delete, a manual `rm -rf` of one path — is
repaired by running this command again. It reports what it found:

```
Deleted 'demo-site'
  store:     was already gone
  published: removed
```

`ARTIFACT_NOT_FOUND` means neither root held it.

## How it runs

Host-local (`_LOCAL_MODULE_COMMANDS`), exec'ing into the toolbox:

```
docker compose <flags> exec -T toolbox node \
  /app/modules/core/versioned_artifacts/toolbox/cli.js --op remove --actor <who>
```

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a traceback.
