# versioned_artifacts

Versioned file trees with mutable **drafts**, immutable **versions**, and an atomic
**current** pointer. Backed by local Git, which is an implementation detail the agent
never sees.

> **Git is an implementation detail — do not expose it.** The public contract is
> `artifact`, `draft`, `version`, `current`, `diff`, `publish`, `revision`. The words
> `repository`, `branch`, `commit`, `merge`, `rebase`, `checkout`, `worktree` and `ref`
> must not appear in a tool name, a parameter, a response field, or an error message.
> `src/agento/toolbox/tests/versioned-artifacts/tools.test.js` asserts this.

## Domain model

| Concept | Identifier | Mutable |
|---|---|---|
| Artifact | `^[a-z0-9][a-z0-9-]{0,63}$` | its `current` pointer |
| Draft | `^d-[a-z0-9]{6,32}$` | yes |
| Version | `^v-\d{8}-\d{6}-[a-z0-9]{4}$` | never |

A version is created by `save_version` and is immutable from that moment. `publish` moves
the artifact's `current` pointer with a compare-and-swap against the version the caller
believed was live; rollback is publishing an older version.

## The ten tools

| Tool | Purpose |
|---|---|
| `versioned_artifact_init` | Create a new EMPTY artifact, scoped to this agent_view (see Scoping below) |
| `versioned_artifact_list` | The artifacts this scope may use, with `current_version` and open drafts |
| `versioned_artifact_get_current` | Which version the artifact publishes |
| `versioned_artifact_list_versions` | Versions, newest first, plus `current_version` |
| `versioned_artifact_create_draft` | Open a draft from `current` or a version, and copy it onto the agent's desk |
| `versioned_artifact_materialize` | Copy a draft or a version onto the desk, replacing what is there |
| `versioned_artifact_save_version` | Copy the desk back and freeze it into a version; the draft stays open |
| `versioned_artifact_diff` | A draft's SAVED content vs `base`, `current`, or a version — desk edits are invisible until saved |
| `versioned_artifact_publish` | Move `current` (CAS on `expected_current_version`) |
| `versioned_artifact_discard_draft` | Throw a draft away |

Eleven names are declared in `module.json`: these ten plus the `versioned_artifact` master
switch each of them `requires`.

**The agent edits files, not the store.** `create_draft` and `materialize` copy a tree
into the agent's workspace — its *desk* — and answer the `path`; the agent then uses the
ordinary file tools, and `save_version` copies the desk back. There is no file-level tool:
the store has no `list_files`, `read_file` or `apply_changes`, so no agent-supplied
filesystem path ever reaches it. `tools.test.js` asserts that no tool schema takes one.

The agent owns the **whole lifecycle** — init → draft → version → hand the code to a
sub-agent → iterate → publish → next draft — with no operator step in it. `get_draft_path`
is internal and is never exposed.

`versioned_artifact_init` takes `artifact_code` and an optional `title`, and nothing else: no
file payload and no path, so the agent fills version 1 through a draft on its desk like any
other change. The CLI keeps the host-directory import — see
[artifact:init](../cli/artifact-init.md).

## Scoping: which artifacts an agent may create and use

A caller may use an artifact when **either** holds:

- the store records that caller's agent_view as its **owner** — written when the artifact
  was created, inside the artifact, by the same guarded step that creates it (an artifact
  exists with its owner, or it does not exist); or
- the code is listed in that scope's `allowed_artifacts`.

An artifact with **no** owner recorded — every one created before this, and every one the
administrative CLI creates — is reachable only through `allowed_artifacts`. That is the same
rule such artifacts already followed, so nothing needs migrating.

The second is the operator's grant, and it is also the **handoff**: one
`config:set versioned_artifacts/allowed_artifacts report --scope agent_view --scope-id 9`
gives view 9 the artifact view 7 created. The value **replaces** that scope's list, so a
second handoff must repeat the earlier codes in the comma-separated value. It is equally the
"specific URL" path — pre-grant `marketing-site` and the agent can `init` it under that exact code.

### The code is a wish; the answer is the identity

`versioned_artifact_init` takes the code the caller **wants** and returns the code it
**got**. They differ when the name is already taken: the answer is `name-2`, then `name-3`.
A caller cannot see what another agent_view took — the store is shared and the listing is
not — so a refusal would be advice it has no way to act on. The suffix is appended, never
spliced over a number the name already ends with, so a taken `plan-2024` becomes
`plan-2024-2` and never `plan-2`.

Two callers are exempt and get `ARTIFACT_ALREADY_EXISTS` instead: the administrative CLI,
and any `init` on a code the operator pre-granted. Both name an address on purpose, and
publishing at a different one would answer a request nobody made.

**Always use the returned code.** Echoing back the requested one addresses the wrong
artifact on every following call.

`limits/max_agent_artifacts` caps creation, counted over the artifacts **that caller may
use** — never over the store. A store-wide count would answer "how many artifacts does every
other agent_view hold", and — with `artifact:delete` reachable only by an operator — it would
let one view lock creation out for everyone until a human intervened. The administrative CLI is exempt from ownership and from the cap.

> **Session-bound identity.** `agent_view_id` comes from the MCP session's capability row
> (AG-16), not from the URL, so a caller cannot name another view. What remains open is the
> co-tenant limit — a run that reads another run's live capability off the shared workspace;
> see [zero-trust.md](../architecture/zero-trust.md).

> **`save_version`, not `publish`, is the HTTP exposure boundary.** Every saved version is
> materialized under `published/<code>/v/<id>/` and is served from that moment; `publish` only
> moves `current`. Enabling `versioned_artifact_save_version` is therefore the decision to let
> that agent_view put bytes on the artifacts port.

> **Delete is the operator's, and only the operator's.** `artifact:delete` removes all three
> places in one step — see [docs/cli/artifact-delete.md](../cli/artifact-delete.md). There is no
> tool equivalent: ownership scopes rather than authorizes, so a self-asserted identity must not
> be able to unmake an immutable history. For an agent the creation cap is therefore still a
> one-way ratchet, and a human is what resets it.

## Error codes

The complete failure vocabulary. Nothing else is ever returned, and a host stack trace
never is.

| Code | Meaning |
|---|---|
| `ARTIFACT_NOT_FOUND` | No such artifact in the store |
| `ARTIFACT_ACCESS_DENIED` | This scope neither owns the artifact nor has it in `allowed_artifacts` |
| `ARTIFACT_LIMIT_REACHED` | This scope is at `limits/max_agent_artifacts` |
| `DRAFT_NOT_FOUND` | Unknown, finished, or half-torn-down draft |
| `DRAFT_LOCKED` | Another operation holds this draft's lock |
| `VERSION_NOT_FOUND` | No such version |
| `VERSION_ALREADY_EXISTS` | Version id collision (retried internally first) |
| `INVALID_PATH` | Malformed identifier, a path outside the desk, or a selector naming both a draft and a version |
| `PATH_OUTSIDE_ARTIFACT` | Path escapes the artifact root |
| `SYMLINK_NOT_ALLOWED` | A symbolic link was encountered |
| `FILE_TOO_LARGE` | Over `limits/max_file_size` |
| `ARTIFACT_TOO_LARGE` | Over `limits/max_total_size` |
| `TOO_MANY_FILES` | Over `limits/max_files` |
| `CURRENT_VERSION_CHANGED` | The CAS on `current` failed — re-read and decide again |
| `STORAGE_OPERATION_FAILED` | Generic storage failure |
| `PUBLISH_FAILED` | Could not move `current`, and it had not moved |
| `WORKSPACE_UNAVAILABLE` | This session has no workspace, so it can hold no desk |
| `DESK_MISSING` | The draft's desk directory is gone — nothing to save |

## Storage layout

Under `storage_root` (default `/srv/versioned-artifacts/store`) — the bind-mounted store
root, mounted into the **toolbox only**:

```
<storage_root>/
  .locks/<artifact_code>.lock        # artifact-creation locks
  audit-fallback.log               # audit rows written when the DB is unreachable
  <artifact_code>/
    repo.git/                      # the storage engine
    worktrees/<draft_id>/          # one checkout per open draft
    locks/                         # artifact.lock and <draft_id>.lock
  .tmp/<random>/                   # materialize's scratch tree, removed unconditionally
```

The **published tree** is the second half of the volume — plain files and no Git, so the
serving container is an ordinary static file server that needs no knowledge of the store.
Its layout maps 1:1 onto the URL path:

```
<published_root>/
  .tmp/<random>/                   # the extraction scratch, swept at boot
  <artifact_code>/
    v/<version_id>/                # one immutable directory per materialized version
    current -> v/<version_id>      # a RELATIVE symlink, swapped atomically
```

`current` is relative so it stays correct whatever absolute path the serving container
mounts the root at. It is installed with `symlink` to a temporary name plus `rename`,
never `mv` — a measured `mv` swap left `current` on the old version and dropped a stray
link inside it.

The agent's **desk** is the other half, and it is on the agent's own workspace, never in
the store:

```
/workspace/artifacts/<workspace>/<agent_view>/<run>/versioned-artifacts/<artifact_code>/<draft_id|version_id>/
```

`<run>` is whatever the MCP URL names: `job_id` for a queued job, `run_id` for an
interactive `agento run`. Both are unique per run, which is the whole requirement — a
shared segment would let two runs write the same desk files, and `save_version` mirrors
the desk, so one run would mint a version from another's bytes and report success. A
session that names neither lands on `/workspace/artifacts/_fallback`, which every such
session shares, and gets `WORKSPACE_UNAVAILABLE` before the store is touched, so it cannot
open a draft it has no way to reach safely. Every desk write is anchored to a
directory **descriptor** opened once, not re-derived from a path, so a directory the
agent replaces with a symlink between the check and the write cannot redirect it.

## Configuration

| Path | Default | Notes |
|---|---|---|
| `versioned_artifacts/storage_root` | `/srv/versioned-artifacts/store` | Absolute; see the single-instance rule below |
| `versioned_artifacts/published_root` | `/srv/versioned-artifacts/published` | Absolute; the tree the artifacts server reads |
| `versioned_artifacts/serving/keep_versions` | `10` | Preview directories kept per artifact. `0` keeps every one; the current target is never pruned |
| `versioned_artifacts/serving/public_base_url` | `http://localhost:8080` | Used to build `preview_url`. Since E1.5 the `artifacts` service publishes no host port, so this URL is **not reachable** until E6 serves versions through the proxy's apps origin. Never set it through `CONFIG__` — ENV beats DB and would kill `config:set` |
| `versioned_artifacts/allowed_artifacts` | *(empty)* | Comma-separated, **on top of** what the scope owns. Scopable to `agent_view`; this is how one view is granted another's artifact |
| `versioned_artifacts/limits/max_file_size` | 5 MiB | |
| `versioned_artifacts/limits/max_total_size` | 100 MiB | |
| `versioned_artifacts/limits/max_files` | 2000 | |
| `versioned_artifacts/limits/max_diff_bytes` | 1 MiB | Diffs past this are truncated, not refused |
| `versioned_artifacts/limits/max_agent_artifacts` | 50 | Artifacts one agent_view may create. Bounds ownership, **not disk** — the store keeps every version, and `keep_versions` bounds only the previews |
| `versioned_artifacts/security/allow_symlinks` | `false` | Only `false` is supported; `true` is rejected at construction |
| `versioned_artifacts/security/basic_auth_default` | `false` | Give newly created artifacts HTTP Basic auth automatically. Off by default; a null is off. Needs `AGENTO_ENCRYPTION_KEY` — see **Basic auth** |

`artifact:init` reads the three import limits from the running toolbox before it
walks `--source`, so raising one with `config:set` takes effect on the admin path too.

A limit that cannot be parsed as a positive integer stops the module at boot rather than
degrading into "no limit".

## Enable checklist

The toolbox image gains `git` with this module, so an existing deployment must rebuild it
before the first `artifact:init` — otherwise every artifact operation fails with
`STORAGE_OPERATION_FAILED`, and the toolbox log reads
`cause: GitFailure code=ENOENT exit=-1` — the spawn found no `git` at all. (The log holds
the OS error code, never git's own output; see the Audit section.) `agento upgrade` rebuilds it;
in the dev stack it is
`cd docker && docker compose -f docker-compose.dev.yml build toolbox && docker compose -f docker-compose.dev.yml up -d toolbox`.
A `restart` is not enough — `git` is an image dependency, not mounted source.

The same upgrade adds the `artifacts` service to the compose file. `docker compose up -d`
creates it; `restart` cannot, because the service did not exist before. It publishes no
host port — see **The serving container**.

Tools are opt-in. The master switch alone leaves all ten children disabled — each is
gated on its own key and merely `requires` the master.

An agent that gets `versioned_artifact_init` needs no artifact created for it and no
`allowed_artifacts` entry — it creates under any free name and owns what it created. The two
lines below are for the other case: an operator-seeded artifact under a code of their choosing.

```bash
uv run bin/agento artifact:init demo-site --source ./some/dir
uv run bin/agento config:set versioned_artifacts/allowed_artifacts demo-site
for t in versioned_artifact versioned_artifact_init versioned_artifact_list versioned_artifact_get_current \
         versioned_artifact_list_versions versioned_artifact_create_draft \
         versioned_artifact_materialize versioned_artifact_save_version \
         versioned_artifact_diff versioned_artifact_publish \
         versioned_artifact_discard_draft; do
  uv run bin/agento config:set "versioned_artifacts/tools/$t/is_enabled" 1
done
```

## Audit

Every mutation writes one row to `versioned_artifact_audit` (`operation`, `artifact_code`,
`draft_id`, `version_id`, `previous_version`, `revision`, `job_id`, `agent_view_id`,
`actor`, `result`, `error_code`, `description`). Failed mutations are recorded too, and
"failed" starts at the outermost boundary: an artifact refused by the allowlist, a draft lock
that could not be taken and a draft that is not there each write their own `result='error'`
row, because a refused attempt is the one an operator most needs to see. The only thing that
writes no row is a syntactically malformed identifier — `artifact_code`, `draft_id` and both
of `publish`'s version ids are checked above the audit boundary, because a malformed one
names nothing that could be audited. Every field that does reach a sink is capped to its
own column width first, so the fallback file holds exactly what the table would have held.
If the INSERT fails, the row is appended to `<storage_root>/audit-fallback.log` instead, so
an event is lost only when both sinks fail — and that is logged.

**What the audit sinks never hold** — and the claim is scoped to them deliberately: file
contents, change message bodies and credentials never reach the `versioned_artifact_audit`
table or `audit-fallback.log`. A change message is not secret-free storage-wide: a
successful `save_version` keeps it in the version history as the revision's own message,
which is the point of passing it. What it must never do is reach the operator log, where a
newline would forge a log record. Two functions in `toolbox/errors.js` enforce that, and
they are not interchangeable: `errorFacts` decides WHICH fields of an error may appear at
all — a class and a machine code, never free-form text and never Git's stderr, because Git
echoes the pathspec it was given — and `boundedLine` caps whatever survives to one bounded
line. The deliberate cost is a thinner diagnostic: a broken store logs
`STORAGE_OPERATION_FAILED: <what failed> — cause: GitFailure exit=128`, and deeper Git output
would need an administrator-only diagnostic path this release does not have.

## Two operational limits worth knowing

- **One toolbox instance per `storage_root`.** The filesystem lock makes *acquire* safe
  between processes, but the startup sweep that clears abandoned locks is a
  check-then-delete pair: a second toolbox sharing the volume can take a lock inside
  that window and have it removed underneath a live mutation. Supporting two instances
  needs a real distributed lock, not a longer stale timeout.
- **A version has no human label in the API.** The `description` passed to `save_version` is
  persisted in `versioned_artifact_audit.description`; `versioned_artifact_list_versions`
  returns `version_id`, `revision` and `preview_path` — no human label. The forward path is an annotated tag object
  per version.

The startup pass reclaims incomplete drafts in **every** artifact the store holds, not
only the allowlisted ones. `allowed_artifacts` decides who may reach an artifact; it is
agent_view-scoped and the toolbox startup pass resolves the default scope only, so a
reclamation gated on it would clear nothing on a normal deployment — and an orphan in
an artifact no view lists would stay forever.

An abandoned lock (its holder died) returns `DRAFT_LOCKED` until the toolbox restarts,
at which point the startup sweep clears it. This is deliberate: `mkdir` gives atomic
acquire but not atomic break, and a waiter that deletes an aged lock can destroy a lock
a third process acquired in the gap.

## Publication and the served tree

`publish` performs three persistent updates, in this order, under the artifact lock:

1. **Materialize** the target version into `<published_root>/<code>/v/<version_id>/`,
   re-materializing it if retention had pruned it. This only writes an immutable
   directory and is idempotent, so a failure here changes nothing anywhere and the
   caller retries with the arguments it already has.
2. **The CAS** on the store's `current` ref — the only serialization point, and it is
   crossed on every call.
3. **The symlink swap**, then retention.

**Recovery contract.** If step 3 fails or the process dies between 2 and 3, the store
says v3 while HTTP still serves v2. The repair is the *ordinary* call with
`expected_current_version` equal to the version you want served:

```bash
uv run bin/agento artifact:publish demo-site v-20260905-154012-a3f2 --expected v-20260905-154012-a3f2
```

Steps 1 and 3 are idempotent, and the three-argument CAS in step 2 is then a no-op that
*verifies* — it succeeds precisely when `current` really is that version and fails with
`CURRENT_VERSION_CHANGED` otherwise. There is deliberately no "skip the CAS when expected
equals the target" shortcut: that would publish a tree the store does not agree with,
which is the divergence this ordering exists to close. A failed swap returns success for
the store change with `preview_stale: true` and a logged warning naming this call — never
a bare success, and never a thrown error that would invite a retry the CAS must refuse.

**Retention** keeps the newest `serving/keep_versions` preview directories per artifact —
`10` by default. Set it to `0` to keep every one. With a positive value the newest N survive, plus whatever
`current` actually resolves to — read from the link itself, never from what a caller
believed it had just installed. A pruned version is still fully readable through
`versioned_artifact_materialize`; only the browser preview is gone, and
`versioned_artifact_list_versions` reports `preview_path: null` for it.

`init` materializes version 1 and points `current` at it, so the store's current and the
served current agree from the artifact's first moment rather than from its first publish.

A `save_version` materializes its new version into the published tree too, and answers
`preview_path`. That step can never fail the save: the version is already in the store,
so a published tree that cannot be written costs the preview and logs, and the save
returns `preview_path: null`.

## The serving container

A fourth compose service, `artifacts`, reads the published tree over HTTP. It runs the
toolbox image with a different command — `server/artifacts-server.js`, plain `node:http`,
no npm dependency — and it is deliberately the least privileged container in the stack:

| | |
|---|---|
| `networks:` | **absent**, so Compose leaves it on the project's `default` network, which it shares with `proxy` alone, while every other service names `agento-net`. Measured: the sandbox cannot resolve the name `artifacts`. |
| `env_file:` / `environment:` | **absent.** It holds no secret and no DB handle. |
| `ports:` | **absent** (removed in E1.5). `proxy` is the only route to the files: the apps origin serves `/a/<code>/v/<version_id>/…` after a `forward_auth` subrequest to `web`, which allows a request only under a live launch (E2, [../architecture/panel.md](../architecture/panel.md)). `preview_url` links and Basic-auth shares stay dark until E6. |
| volumes | `storage/versioned-artifacts/published` (read-only), `app/etc` (read-only), and the modules tree. Never the store root. |

The absence of `networks:` is the point, not an oversight. One line added for consistency
would put every artifact on `agento-net`, where every agent in every agent_view could read
every artifact over plain HTTP — `allowed_artifacts` bypassed for reads, silently, with no
audit row. The comment above the service in both compose files says so. Note what "absent"
actually buys: Compose still gives the service the project's `default` network, so the
isolation is that it is **not on `agento-net`**, not that it has no network at all.

**Routes.** `/` lists the artifact codes. `/<code>/…` serves through the `current` symlink.
`/<code>/v/<version_id>/…` serves one immutable version. A pruned version falls through to
the ordinary 404, and a directory URL without a trailing slash answers `301` to the
slash-terminated one, so a relative `app.js` resolves inside the directory. Every path is
checked with `realpath` against the root before anything is read, so a symlink inside a
published tree cannot escape it; dotfiles are refused outright.
A transient `EINVAL`/`ESTALE` — measured on the macOS VirtioFS mount on the first request
after a symlink swap — is retried once before the answer.

**Disabling the module stops the serving.** `app/etc/modules.json` is mounted read-only and
re-read per request (stat-cached on mtime), so `agento mo:di versioned_artifacts` turns
every route, `/` included, into a `503` without a restart and without deleting anything;
`mo:en` brings it back the same way. Absent file, absent key or unparseable file mean
**serve**: that file lists only explicitly toggled modules, so absence is "enabled". This
mirrors module enablement, **not** the `is_enabled` tool gate, which is the one that fails
closed.

The container is added to the compose file unconditionally — `regenerate_compose` has no
per-module service mechanism — so a deployment with the module disabled still *runs* the
container; what the gate guarantees is that it no longer *serves*. See DECISIONS.md.

## Basic auth

An artifact can gate its served pages behind HTTP Basic auth. The password is kept in two
forms that never meet, because the container that *enforces* auth is the one with no secret:

* the **serving container** reads only a one-way scrypt hash from `published/<code>/.auth`
  — a sibling of `v/` and `current`, so retention and the `current` swap never touch it, and
  a dotfile the server refuses to serve. It holds no encryption key and no DB handle, so a
  hash is the only credential it can be trusted with;
* the **`versioned_artifact` row** keeps the password AES-encrypted (the same `obscure`
  mechanism as config), so an operator can read it back or rotate it.

The sidecar is what the server actually enforces, so it is written first; a failed DB
upsert costs only the recoverable copy, never the protection. A present-but-corrupt `.auth`
fails **closed** (`401`), never open. `/` stays open so the container healthcheck keeps
passing — auth covers everything under `/<code>/`.

`security/basic_auth_default` (off by default; a null is off) decides whether a **new**
artifact is gated automatically. When it is, `versioned_artifact_init` returns the
credential **once** so the agent can hand it to the user. Enabling it needs
`AGENTO_ENCRYPTION_KEY`; without it the artifact is still created, just served open.

Setting, rotating, showing or disabling auth is operator-only — the
[`artifact:auth`](../cli/artifact-auth.md) CLI, with **no tool equivalent**, so a
self-asserted `agent_view_id` can never change who may read a published tree. Each change
writes a `versioned_artifact.auth.set` audit row.

## Deviations from the PRD

1. The module ships in `src/agento/modules/` (a core module), not `app/code/`.
2. The service and the storage backend are JavaScript in the toolbox — there is no
   JS→Python call path from an MCP tool.
3. `git` is installed in the toolbox image and a dedicated `storage/versioned-artifacts`
   volume is mounted into the toolbox only. `isomorphic-git` has no linked-worktree API
   and cannot back drafts.
4. Artifact creation is a host command over `docker compose exec`, not an HTTP route: the
   toolbox authenticates no caller, so any route on that listener is agent-callable.
5. Audit goes to a new `versioned_artifact_audit` table.
6. Agent-facing documentation ships through `workspace/`, not `knowledge/`.
7. `allow_symlinks: true` is configuration-only and is rejected at service construction.

## Out of scope for this release

Garbage collection of unreferenced Git objects, and human-in-the-loop publication
approval beyond the agent instruction text. Preview retention, the published tree and the
serving container are all in — see **Publication and the served tree** and **The serving
container** above.

The server answers `GET` and `HEAD` only, and implements no `Range`, `ETag` or conditional
requests: an artifact preview is small and re-read rarely, and adding them would have meant
a dependency that cannot be tested here (DECISIONS.md, 2026-09-12). There is also no TLS
and no authentication — the loopback bind *is* the access control, so `serving/public_base_url`
must not be pointed at a public address without a reverse proxy in front.
