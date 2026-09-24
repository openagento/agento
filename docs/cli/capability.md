# capability:mint / capability:revoke

Issue and retire **toolbox capability tokens** — the bearer credential every request to the toolbox
must carry. The toolbox derives the request scope (`agent_view_id`, `job_id`) from the capability row,
so a caller can never widen its own scope by asking for a different view.

Both commands are for operators and for scripts (`docker/smoke/toolbox-capability-smoke.sh`). Normal
runs need neither: the consumer mints a capability per job and revokes it on the terminal transition,
and `agent_view:prepare-run` mints one per interactive run.

**Python callers do not mint by hand either.** A publisher, channel or onboarding flow that needs an
`internal_rest` token uses the `rest_capability(agent_view_id=…, subject_id=…, db_config=…)` contextmanager from
`agento.framework.toolbox_capability`, and builds its HTTP client INSIDE the `with`:

```python
with rest_capability(agent_view_id=av.id, subject_id="service:jira", db_config=db_config) as capability_token, closing(
    SomeToolboxClient(toolbox_url, capability_token=capability_token)
) as client:
    client.do_work(av.id)
```

Revocation is then guaranteed on every exit — including a client constructor that raises and a
`close()` that fails, the two paths a hand-written `try/finally` around a pre-built client jumps
over. A revoke that fails while another error is already in flight is logged as a category and never
masks that error; a revoke that fails on the success path raises `CapabilityRevokeError`, which
carries the failure category and never the driver message.

The helper takes **no connection** — it opens its own from `db_config`, because the toolbox
validates the token from another process on another connection: the row must be durable before the
first request, so the mint cannot ride the caller's transaction, and committing on a borrowed
connection would publish whatever else that caller had pending. It holds that connection only for
the write: the mint closes it and the revoke opens a second one, so no block pins a pooled
connection it never uses. Pass the `db_config` you already hold; omitting it falls back to
`DatabaseConfig.from_env()`, which is right inside the containers and wrong anywhere the caller was
given a different database.

**The block must be one bounded request.** An `internal_rest` token lives 120 seconds. A block that
waits for a human, sleeps a backoff, or drives a whole agent run hands that work an already dead
credential — and keeps a live bearer in existence for the whole wait, while nothing is using it.
Collect the input first, then mint; a retry mints again. For a flow with several such steps, take a
`capability_client` **opener** instead of a client and open one per step:

```python
from agento.framework.toolbox_capability import capability_client

toolbox = capability_client(
    lambda token: SomeToolboxClient(toolbox_url, capability_token=token),
    agent_view_id=av.id, db_config=db_config,
)

workspace = input("  Workspace: ")          # no capability exists while the operator types
with toolbox() as client:                   # minted here …
    result = client.verify(workspace)       # … used for exactly this call …
                                            # … and revoked here
```

Nothing is minted when the opener is built, and no two uses share a token, so a retry after a
failed attempt is authorized by a capability of its own.

## Usage

```bash
agento capability:mint --kind <kind> --agent-view <code> [--transport http|sse|both] [--ttl <seconds>]
agento capability:revoke   # reads the raw token from stdin
```

Shortcuts: `cap:mi`, `cap:re`.

## Kinds

| Kind | Who holds it | Reaches | Max / default TTL |
|------|--------------|---------|-------------------|
| `internal_rest` | Python publishers, channels, onboarding, `config:test` | `/api/*`, scoped `/health`, `/config-test` | 120 s |
| `mcp_interactive` | an interactive `agento run` session | `/mcp`, `/sse`, `/messages`, invoke | 43200 s (12 h); 4 h with `sse` |
| `mcp_job` | a consumer-run job | `/mcp`, `/sse`, `/messages`, invoke | issued by the consumer only |

An `internal_rest` capability may be **viewless** (`agent_view_id` NULL, no job) for one purpose:
`config:test` at the default scope. Only `/config-test` accepts one — every other guard refuses a
row without a view, so it can never reach global config through `/api`, `/mcp` or `/health`.

`mcp_job` is **not** a valid `--kind`. Its lifetime is bound to the job's terminal transition; a
hand-minted one would outlive the code that revokes it.

`--ttl` must be between `1` and the kind's maximum. A larger value is an **error**, not a silent
clamp — an operator who asks for 24 h and receives 2 min would deploy against the wrong assumption.
Omit `--ttl` to get the maximum.

`--transport` sets the token's `allowed_transports` (default `http`). `http` is the
`Authorization: Bearer` header on `/mcp`, `/api`, `/config-test` and `/health`; `sse` is `/sse` and
`/messages`, where the token travels as `?cap=`. A token that may travel on `sse` lives at most
4 hours, because a query string lands in access logs. `internal_rest` is `http` only — the toolbox
refuses it anywhere else. A token with no `allowed_transports` fails verification at every endpoint.

## Output

`capability:mint` prints **the token and nothing else** on stdout, so it composes through a pipe:

```bash
umask 077
agento capability:mint --kind internal_rest --agent-view dev \
  | sed 's/^/header = "Authorization: Bearer /; s/$/"/' > "$TMPDIR/cap.curlrc"
curl --config "$TMPDIR/cap.curlrc" \
     -H 'content-type: application/json' \
     -d '{"jql": "project = AG order by created"}' \
     http://toolbox:3001/api/jira/search
rm -f "$TMPDIR/cap.curlrc"
```

Everything a human reads goes to stderr. The token is never logged and never put in argv.

**Do not write `curl -H "Authorization: Bearer $TOKEN"`.** A shell expands `$TOKEN` into `curl`'s
argv, which `ps` and the audit log read for the whole life of the request — the same leak
`capability:revoke` avoids by taking the token on stdin. Use `curl --config` with a mode-0600 file
(`umask 077` above), or `--config -` to keep the token out of the filesystem too. The same rule
covers every other client: the token travels on **stdin or a mode-0600 file**, never an argument.

## Revoking

```bash
agento capability:mint --kind internal_rest --agent-view dev | agento capability:revoke
```

The token arrives on **stdin**, never as an argument — argv is world-readable through `ps` and lands
in shell history. There is deliberately no `--id`: the table stores SHA-256 hashes only, so an id
would need a listing that correlates operators with live capabilities for no gain.

After revocation the toolbox answers `403` (the token is present but no longer valid). A **missing**
token is `401`.

## Storage and expiry

`toolbox_capability` holds the hash, kind, `agent_view_id`, optional `job_id`, `expires_at` and
`revoked_at` — never the raw token. Expired rows are purged by the consumer on an idle tick, at most
once per hour.

A capability confines a **request**, not a **host**. Concurrent runs on one deployment share the
`agent` account and the workspace mount, so a shell-capable agent can read a co-tenant's live token
off disk and authenticate as that view — see the co-tenant paragraph in
[zero-trust.md](../architecture/zero-trust.md) before treating view separation as a boundary between
mutually hostile tenants.

## See also

- [docs/architecture/zero-trust.md](../architecture/zero-trust.md) — why the toolbox trusts nothing
  that the caller says about itself
- [docs/cli/run.md](run.md) — interactive runs and the capability they carry
