# versioned_folders

Versioned file trees with mutable **drafts**, immutable **versions**, and an atomic
**current** pointer. Backed by local Git, which is an implementation detail the agent
never sees.

> **Git is an implementation detail — do not expose it.** The public contract is
> `folder`, `draft`, `version`, `current`, `diff`, `publish`, `revision`. The words
> `repository`, `branch`, `commit`, `merge`, `rebase`, `checkout`, `worktree` and `ref`
> must not appear in a tool name, a parameter, a response field, or an error message.
> `src/agento/toolbox/tests/versioned-folders/tools.test.js` asserts this.

## Domain model

| Concept | Identifier | Mutable |
|---|---|---|
| Folder | `^[a-z0-9][a-z0-9-]{0,63}$` | its `current` pointer |
| Draft | `^d-[a-z0-9]{6,32}$` | yes |
| Version | `^v-\d{8}-\d{6}-[a-z0-9]{4}$` | never |

A version is created by `finalize` and is immutable from that moment. `publish` moves
the folder's `current` pointer with a compare-and-swap against the version the caller
believed was live; rollback is publishing an older version.

## The ten tools

| Tool | Purpose |
|---|---|
| `versioned_folder_get_current` | Which version the folder publishes |
| `versioned_folder_create_draft` | Open a draft from `current` or a version |
| `versioned_folder_list_files` | List a draft's or a version's files |
| `versioned_folder_read_file` | Read one file (binary files report `encoding: binary`) |
| `versioned_folder_apply_changes` | Writes + deletes as one atomic batch (each path once, in one list) |
| `versioned_folder_diff` | Draft vs `base`, `current`, or a version |
| `versioned_folder_finalize` | Freeze a draft into a version |
| `versioned_folder_publish` | Move `current` (CAS on `expected_current_version`) |
| `versioned_folder_list_versions` | Versions, newest first, plus `current_version` |
| `versioned_folder_discard_draft` | Throw a draft away |

Folder creation is **not** a tool — see [versioned-folder:init](../cli/versioned-folder-init.md).
`get_draft_path` is internal and is never exposed.

## Error codes

The complete failure vocabulary. Nothing else is ever returned, and a host stack trace
never is.

| Code | Meaning |
|---|---|
| `FOLDER_NOT_FOUND` | No such folder in the store |
| `FOLDER_ACCESS_DENIED` | The folder is not in this scope's `allowed_folders` |
| `DRAFT_NOT_FOUND` | Unknown, finished, or half-torn-down draft |
| `DRAFT_LOCKED` | Another operation holds this draft's lock |
| `DRAFT_HAS_NO_CHANGES` | Nothing to save or nothing to finalize |
| `VERSION_NOT_FOUND` | No such version |
| `VERSION_ALREADY_EXISTS` | Version id collision (retried internally first) |
| `FILE_NOT_FOUND` | No such file in the draft or version |
| `INVALID_PATH` | Malformed identifier or path, a backslash, or one path named twice in a batch (aliases such as `a//b.txt` count as the same name) |
| `PATH_OUTSIDE_FOLDER` | Path escapes the folder root |
| `SYMLINK_NOT_ALLOWED` | A symbolic link was encountered |
| `FILE_TOO_LARGE` | Over `limits/max_file_size` |
| `FOLDER_TOO_LARGE` | Over `limits/max_total_size` |
| `TOO_MANY_FILES` | Over `limits/max_files` |
| `CURRENT_VERSION_CHANGED` | The CAS on `current` failed — re-read and decide again |
| `GIT_OPERATION_FAILED` | Generic storage failure |
| `FINALIZE_FAILED` | The version exists; closing the draft did not finish |
| `PUBLISH_FAILED` | Could not move `current`, and it had not moved |

## Storage layout

Under `storage_root` (default `/srv/versioned-folders`, mounted into the **toolbox
only**):

```
<storage_root>/
  .locks/<folder_code>.lock        # folder-creation locks
  audit-fallback.log               # audit rows written when the DB is unreachable
  <folder_code>/
    repo.git/                      # the storage engine
    worktrees/<draft_id>/          # one checkout per open draft
    locks/                         # folder.lock and <draft_id>.lock
```

## Configuration

| Path | Default | Notes |
|---|---|---|
| `versioned_folders/storage_root` | `/srv/versioned-folders` | Absolute; see the single-instance rule below |
| `versioned_folders/allowed_folders` | *(empty)* | Comma-separated; empty denies everything. Scopable to `agent_view` |
| `versioned_folders/limits/max_file_size` | 5 MiB | |
| `versioned_folders/limits/max_total_size` | 100 MiB | |
| `versioned_folders/limits/max_files` | 2000 | |
| `versioned_folders/limits/max_diff_bytes` | 1 MiB | Diffs past this are truncated, not refused |
| `versioned_folders/security/allow_symlinks` | `false` | Only `false` is supported; `true` is rejected at construction |

`versioned-folder:init` reads the three import limits from the running toolbox before it
walks `--source`, so raising one with `config:set` takes effect on the admin path too.

A limit that cannot be parsed as a positive integer stops the module at boot rather than
degrading into "no limit".

## Enable checklist

The toolbox image gains `git` with this module, so an existing deployment must rebuild it
before the first `versioned-folder:init` — otherwise every folder operation fails with
`GIT_OPERATION_FAILED`, and the toolbox log reads
`cause: GitFailure code=ENOENT exit=-1` — the spawn found no `git` at all. (The log holds
the OS error code, never git's own output; see the Audit section.) `agento upgrade` rebuilds it;
in the dev stack it is
`cd docker && docker compose -f docker-compose.dev.yml build toolbox && docker compose -f docker-compose.dev.yml up -d toolbox`.
A `restart` is not enough — `git` is an image dependency, not mounted source.

Tools are opt-in. The master switch alone leaves all ten children disabled — each is
gated on its own key and merely `requires` the master.

```bash
uv run bin/agento versioned-folder:init demo-site --source ./some/dir
uv run bin/agento config:set versioned_folders/allowed_folders demo-site
for t in versioned_folders versioned_folder_get_current versioned_folder_create_draft \
         versioned_folder_list_files versioned_folder_read_file versioned_folder_apply_changes \
         versioned_folder_diff versioned_folder_finalize versioned_folder_publish \
         versioned_folder_list_versions versioned_folder_discard_draft; do
  uv run bin/agento config:set "versioned_folders/tools/$t/is_enabled" 1
done
```

## Audit

Every mutation writes one row to `versioned_folder_audit` (`operation`, `folder_code`,
`draft_id`, `version_id`, `previous_version`, `revision`, `job_id`, `agent_view_id`,
`actor`, `result`, `error_code`, `description`). Failed mutations are recorded too, and
"failed" starts at the outermost boundary: a folder refused by the allowlist, a draft lock
that could not be taken and a draft that is not there each write their own `result='error'`
row, because a refused attempt is the one an operator most needs to see. The only thing that
writes no row is a syntactically malformed identifier — `folder_code`, `draft_id` and both
of `publish`'s version ids are checked above the audit boundary, because a malformed one
names nothing that could be audited. Every field that does reach a sink is capped to its
own column width first, so the fallback file holds exactly what the table would have held.
If the INSERT fails, the row is appended to `<storage_root>/audit-fallback.log` instead, so
an event is lost only when both sinks fail — and that is logged.

**What the audit sinks never hold** — and the claim is scoped to them deliberately: file
contents, change message bodies and credentials never reach the `versioned_folder_audit`
table or `audit-fallback.log`. A change message is not secret-free storage-wide: a
successful `apply_changes` keeps it in the version history as the revision's own message,
which is the point of passing it. What it must never do is reach the operator log, where a
newline would forge a log record. Two functions in `toolbox/errors.js` enforce that, and
they are not interchangeable: `errorFacts` decides WHICH fields of an error may appear at
all — a class and a machine code, never free-form text and never Git's stderr, because Git
echoes the pathspec it was given — and `boundedLine` caps whatever survives to one bounded
line. The deliberate cost is a thinner diagnostic: a broken store logs
`GIT_OPERATION_FAILED: <what failed> — cause: GitFailure exit=128`, and deeper Git output
would need an administrator-only diagnostic path this release does not have.

## Two operational limits worth knowing

- **One toolbox instance per `storage_root`.** The filesystem lock makes *acquire* safe
  between processes, but the startup sweep that clears abandoned locks is a
  check-then-delete pair: a second toolbox sharing the volume can take a lock inside
  that window and have it removed underneath a live mutation. Supporting two instances
  needs a real distributed lock, not a longer stale timeout.
- **A version has no human label in the API.** The `description` passed to `finalize` is
  persisted in `versioned_folder_audit.description`; `versioned_folder_list_versions`
  returns only `version_id` and `revision`. The forward path is an annotated tag object
  per version.

The startup pass reclaims incomplete drafts in **every** folder the store holds, not
only the allowlisted ones. `allowed_folders` decides who may reach a folder; it is
agent_view-scoped and the toolbox startup pass resolves the default scope only, so a
reclamation gated on it would clear nothing on a normal deployment — and an orphan in
a folder no view lists would stay forever.

An abandoned lock (its holder died) returns `DRAFT_LOCKED` until the toolbox restarts,
at which point the startup sweep clears it. This is deliberate: `mkdir` gives atomic
acquire but not atomic break, and a waiter that deletes an aged lock can destroy a lock
a third process acquired in the gap.

## Deviations from the PRD

1. The module ships in `src/agento/modules/` (a core module), not `app/code/`.
2. The service and the storage backend are JavaScript in the toolbox — there is no
   JS→Python call path from an MCP tool.
3. `git` is installed in the toolbox image and a dedicated `storage/versioned-folders`
   volume is mounted into the toolbox only. `isomorphic-git` has no linked-worktree API
   and cannot back drafts.
4. Folder creation is a host command over `docker compose exec`, not an HTTP route: the
   toolbox authenticates no caller, so any route on that listener is agent-callable.
5. Audit goes to a new `versioned_folder_audit` table.
6. Agent-facing documentation ships through `workspace/`, not `knowledge/`.
7. `allow_symlinks: true` is configuration-only and is rejected at service construction.

## Out of scope for this release

Garbage collection and retention policy, HTTP/Artifact serving of a version, and
human-in-the-loop publication approval beyond the agent instruction text.
