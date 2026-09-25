# artifact:list

List the versioned artifacts the store holds, with their metadata and preview URL.

```bash
uv run bin/agento artifact:list [--actor <who>]
```

One row per artifact: `artifact_code`, current version, title, owner, `created_at`,
preview URL. Any missing metadata prints as `-`. The preview URL is built from
`serving/public_base_url`. Since E1.5 the `artifacts` container publishes no host port, so
the URL is not reachable until E6 serves versions through the proxy's apps origin.

## Why it sees every artifact

An agent reaches an artifact it created itself (the store records the owning agent_view) or through
`versioned_artifacts/allowed_artifacts`, which is `agent_view`-scoped and empty by default.
This command runs as an administrator and is exempt from both, so the store — not a scope —
is what it lists. That exemption is set only by the toolbox CLI; the MCP tool layer never
passes it, so nothing about the agent's access changes.

The `versioned_artifact` table only decorates the listing. The store is the authority on
what exists, so a table that cannot be read costs a title, never an artifact.

## How it runs

Like `artifact:init`, it is host-local (`_LOCAL_MODULE_COMMANDS`) and execs into the
toolbox, which is the only container that can reach the store:

```
docker compose <flags> exec -T toolbox node \
  /app/modules/core/versioned_artifacts/toolbox/cli.js --op list --actor <who>
```

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a traceback.
A reply without an artifact list is a failure, not an empty deployment.
