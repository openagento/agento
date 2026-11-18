# The composer: a send/stop toggle, and file upload

Status: settled with the owner on 2026-11-18 (see Settled). Branch `panel-rail-polish` (or a
branch off it). Two commits, in this order: **A** the send/stop toggle, **B** the upload.

## Goal

1. **A.** The Stop control stops being a floating `Button` under the status line and becomes the
   send button itself: while a turn runs, the pill's submit icon is a square **Stop**.
2. **B.** A panel user hands the agent a file: a `+` in the composer, or a drag&drop onto the
   conversation pane. It works for a thread whose turn is running, for an idle thread, and for a
   thread whose first message has not been sent yet.

## Verified facts (PLN-1)

| Fact | Proof |
|---|---|
| The composer is a generic, panel-and-kit component; the pill holds the textarea and one submit `ActionIcon` | `ChatComposer`, `frontend/packages/ui/src/components/Chat.tsx:190-219` |
| Typing is already allowed while the agent answers; only `canSend` waits | `Chat.tsx:188-194`, `ConversationsPage.tsx:184-202` (`Composer`) |
| Stop today is a `Button` rendered under `ChatStatus`, from the page, not the composer | `src/agento/modules/conversation/panel/ConversationsPage.tsx:594-598` |
| The stop mutation posts `/api/conversation/threads/:id/stop` | `ConversationsPage.tsx:394-400`; route `src/agento/modules/conversation/di.json` (`src.routes.stop`), handler `src/agento/modules/conversation/src/routes.py:293` |
| `ChatStatus` has an unused optional `action` slot | `Chat.tsx:155` |
| Sending a message is `POST /api/conversation/threads/:id/messages`, JSON `{content, client_message_id}` | `ConversationsPage.tsx:166-168`; `src/agento/modules/conversation/src/routes.py:340` |
| Every `web` route with a body is JSON-only; a non-JSON content type is 400 | `src/agento/web/server.py:275-278` (`_read_body`) |
| The JSON body cap is 64 KB | `src/agento/web/server.py:48,362` (`MAX_JSON_BODY`) |
| There is no multipart, upload or attachment path anywhere in `web` or in a module `di.json` | `grep -rn "multipart" src/agento` (2026-11-18): no hit |
| `web` mounts the workspace read-write and runs as `agent` | `docker/docker-compose.dev.yml:199,226` |
| The thread's `shared/` is mode 2770, group `THREAD_GID`, and is created by **run preparation**, not by `web` | `src/agento/framework/artifacts_dir.py:89,126-153`; `src/agento/framework/runner/uids.py:40`; called at `src/agento/framework/run_preparation.py:298` |
| The run's `./shared` is a symlink into the thread dir, made in the same call | `src/agento/framework/run_preparation.py:190,296-298` |
| The job's prompt is built in one place, for the panel branch | `src/agento/modules/conversation/src/service.py:1196-1207` (read 2026-11-18) (`complete_pending` → `publish_job(prompt=row["content"], …)`) |
| **The job id does not exist when that prompt is built** — `publish_job` returns it | same, `service.py:1198` |
| The message id *does* exist before the publish (`submit_message` inserts `pending`, `complete_pending` reads it) | `service.py:1049,1170-1189` |
| The module already sweeps unfinished submissions on a cron minute | `service.py:1260` (`sweep_pending`); `src/agento/modules/conversation/cron.json` (`conversation_sweep`) |
| Module limits live in `config.json` (e.g. `limits/max_message_bytes`) | `src/agento/modules/conversation/config.json` |
| The runner mounts the **whole** workspace, like `web`: a dir `web` writes at `workspace/uploads/` is visible in the runner, on the same bind mount | `docker/docker-compose.dev.yml:139` (runner-1) vs `:226` (web) |
| The runner is root with `cap_drop: ALL` and **no `DAC_OVERRIDE`** — it may read and search any dir (`DAC_READ_SEARCH`) and `chown` (`CHOWN`), but it may **not** write into a dir it does not own | `docker/docker-compose.dev.yml:146-147` |
| `publish_job_on` has no field that could carry an attachment list | `src/agento/framework/publish_service.py:23-45` (the full keyword list) |
| `job` has `context TEXT` and `prompt MEDIUMTEXT`, both already meaningful | `src/agento/framework/sql/001_create_tables.sql:21`, `008_job_prompt_output.sql:2` |
| A file reaches a run today only because a tool fetched it (Jira, Outlook); no user-to-agent path exists | `src/agento/modules/jira/toolbox/jira.js` (`attachmentTarget`), `src/agento/modules/outlook/toolbox/outlook.js` (`outlook_get_attachment`) — reported by the explore pass, not re-read line by line |

`ASSUMPTION` — the per-run sandbox does not expose `/workspace/uploads/` to the **agent**
process (the runner container sees it; the question is what the run's namespace sees).
**Spike before B-3:** read the per-run mount/namespace set in `framework/runner/`, and add a
test that asserts a run cannot read another message's staging dir. If it is reachable, the
staging dir moves under a mode-0700 root-owned parent and `web` writes through a dir it owns
inside it.

## Placement (PLC-1)

| Unit | Runs | Owner | Why |
|---|---|---|---|
| send/stop toggle | browser | `frontend/packages/ui` (`ChatComposer`) + `conversation/panel` | a generic chat primitive; the page keeps the mutation |
| upload route + staging write | Python, `web` | `conversation` module | no tool credential, no MCP tool → Python (PLC-1 q1→2); it is the panel conversation's contract → its module (q7) |
| moving a staged dir into the thread's `shared/` | Python, runner, as root before the drop | **framework** (`run_preparation` / `artifacts_dir`) | it is a lifecycle step over a thread dir that only the framework may touch, and it names no vendor (q5) |
| the `Attached files:` block in the prompt | Python, `web`/`cron` | `conversation` module | it is the meaning of a panel message |

No new toolbox code, no new tool, no new MCP surface (TBX untouched).

## Scope

### In — commit A: the send/stop toggle

1. `ChatComposer` takes two new optional props: `stoppable?: boolean` and
   `onStop?: () => void`. It is deliberately **not** driven by the existing `busy`: `busy` is
   derived from the status text and can be true with no `RUNNING`/`PAUSED` turn, which would
   show a Stop that answers 409 (`service.stop` raises "no turn of this thread is running").
   `stoppable` is the page's existing flag (`ConversationsPage.tsx:594`).
   When `stoppable && onStop`, the submit `ActionIcon` becomes `type="button"`, label **Stop**, a
   square icon, and calls `onStop()`. Otherwise it is the send button as today. Enter while
   busy does **not** stop — Enter only ever sends (`ready` is unchanged).
2. `ConversationsPage`: the `Button` at `:594-598` is deleted; `stoppable`, `stop.isPending` and
   `stop.mutate` are passed down into `Composer` → `ChatComposer`. `stop.error` keeps its
   place. `ChatStatus` keeps its text ("Starting…", "Thinking…").
3. The textarea stays enabled while busy (already true, `Chat.tsx:188`).
4. A channel thread with a running turn also shows Stop today (`conversation.test.tsx:531`) —
   the `ChannelComposer` gets the same two props, so that test keeps its meaning.

### In — commit B: file upload

1. **Route.** `POST /api/conversation/threads/:id/uploads`, declared in `di.json` with
   `acl: "self"` and **without** `json_body`, plus a new per-route body limit. The handler
   calls `_visible(req)` + `_write_refusal(req, row)` exactly as `send` does
   (`routes.py:340`, `_write_refusal` at `routes.py:310`) — same 404 / refusal shape (SEC-7, CLS-1).
2. **Body.** `multipart/form-data`, one file per request, parsed with the stdlib
   (`email.parser` over the reconstructed part, or `cgi`-free manual boundary split —
   decided in implementation; no new dependency, SEC-11). `web/server.py` grows one
   capability: a route may declare `body: "multipart"`, which sets its own byte limit and
   skips the `application/json` check. The JSON default is unchanged for every existing route
   (SEC-9: a route with no declaration is still JSON-only).
3. **Staging.** The file is written to `workspace/uploads/<upload_id>/<safe-name>`, where
   `upload_id` is a server-minted 32-hex id, mode 0700 dir / 0600 file, owner `agent`.
   `safe-name`: basename only, no separator, no leading dot, NFC, ≤120 bytes, a taken name
   gets `-2` (SEC-5).
4. **Reply** `201 {upload_id, name, bytes}`.
5. **Send.** `POST …/messages` additionally accepts `upload_ids: string[]` (≤10). Each id is
   validated to be one this user staged for this thread, and is recorded with the message.
   The names are stored in the existing `message` row — a new `uploads JSON NULL` column
   (`sql/0NN_message_uploads.sql`, MOD-3) — so the sweep, the prompt and a retry see the same
   set after the staged dirs have been folded into `m<message_id>/`.
6. **Prompt.** `complete_pending` appends to `row["content"]`, for the panel branch only:

   ```
   <message>

   Attached files:
   - ./shared/attachments/<upload_id>/<name>
   ```

   One dir per **turn**, keyed by `message_id` — not by the job id, which does not exist when
   the prompt is built (Verified facts), and not by `upload_id`, which would scatter one
   turn's files over ten dirs. At send, `web` (the owner of the staged dirs) renames each
   `workspace/uploads/<upload_id>` into `workspace/uploads/m<message_id>/<name>`. The channel
   branch (`service.py:1215`) is untouched.
7. **Delivery.** The list must survive from publish (in `web`/`cron`) to run preparation (in
   the runner, at claim), and the framework may not read the module's `message` table (MOD-1).
   So: one framework migration adds `job.attachments VARCHAR(255) NULL` — a single staging dir
   name, vendor-free, no conversation concept — and `publish_job_on` gains the matching keyword
   (`publish_service.py:23-45`). `complete_pending` passes `m<message_id>`.

   Run preparation, still root, **copies** that dir into
   `<thread shared>/attachments/<message_id>/` and chowns it to the run uid + `THREAD_GID`
   (mode 2770 / 0640), beside `prepare_thread_dir` (`run_preparation.py:298`). It copies and
   does not move: the runner has `DAC_READ_SEARCH` and `CHOWN` but **no `DAC_OVERRIDE`**
   (Verified facts), so it may read the `agent`-owned staging dir but may not unlink inside
   it. The staging copy is removed later by the sweep, which runs as the uid that owns it.
   The copy is idempotent — a retry of the same job finds the target dir and does nothing.
   A job with no thread gets `<run dir>/shared/attachments/<message_id>/` — the panel always
   has a thread, so that is the fail-safe branch, not a feature.
8. **Composer UI.** A `+` `ActionIcon` on the left of the pill (`UI-7`: icon with a title),
   a hidden `<input type="file" multiple>`, and a drop zone over the whole conversation pane
   with a dashed overlay on drag-over. Each staged file is a chip under the textarea with a
   name, a progress state and a remove `IconAction`. A failed upload shows its error on the
   chip and the chip stays removable. Every visual value comes from `theme.ts` (UI-1).
9. **Limits** (`config.json`, CFG-1): `limits/upload_max_bytes` 26214400 (25 MB),
   `limits/uploads_per_message` 10, `limits/upload_retention_hours` 24. No type allow-list:
   the panel never renders the bytes and the agent reads them as a file.
10. **Sweep.** `conversation:sweep` (already a cron minute) removes a staging dir when either:
    it is an `<upload_id>` dir older than `limits/upload_retention_hours` that no message
    claimed (the composer was abandoned), or it is an `m<message_id>` dir whose message has a
    `job_state` of `published`/`terminal` **and** whose job has left `TODO`/`RUNNING` — the
    run has its copy. A dir whose message has not run yet is never swept, whatever its age
    (an un-run message may wait behind a long turn).
11. **Delivered dirs** are the thread's, so thread retention already removes them with the
    thread; nothing new there.

### Out of scope

- An attachment chip in the message history, or downloading the file back from the panel. The
  names are in the message text, which is enough to read the history. → ROADMAP.
- The `ChannelComposer` (a Jira/Outlook reply): a file there must be uploaded to *that*
  system. → ROADMAP.
- Any tool (`list_attachments`): the agent can `ls` the directory it is told about (CODE-3).
- Image rendering, previews, paste-from-clipboard.
- A per-agent_view override of the limits.

## Extension points (EVT-1)

- Staging an upload: `no event` — it is not a state change of a persisted thing; the message
  it belongs to has its own events already.
- Attaching uploads to a message: `no event` — the message's existing publication path
  (`complete_pending` → `job.queued`) is the state change, and it is unchanged in shape.
- Delivering a staged dir into a thread: `no event` — it is a step inside run preparation,
  which dispatches nothing today (EVT-3: run preparation is root).

## Security notes

- The file comes from the **trusted** side: a `web` session the panel authenticated, with the
  thread's own write gate (SEC-7). It never passes a toolbox capability, so no agent-side
  credential is involved (SEC-1).
- The agent receives a path, never a handle to the staging area; the move is done by root in
  run preparation, and the agent's own uid never reaches `workspace/uploads/` (the
  `ASSUMPTION` above is the proof obligation).
- Names are sanitized before they touch the filesystem and before they are written into the
  prompt (SEC-5). `web` checks its rate-limit buckets before it resolves a route
  (`server.py:139-149`), so the new route is covered with no opt-in (SEC-12); whether that
  check bounds *bytes* and not only requests is a question for the implementation review,
  because this route raises the per-request cost by three orders of magnitude.
- An error from the route says "upload failed" and a reason class, never a path (SEC-6).

## Scale (SCL-1)

The upload route is per-user, not per-run; it adds no shared lock and no per-run hot-path
write. Run preparation gains one `rename()` per job that has attachments, inside the work it
already does. No new bound is needed beyond `uploads_per_message` and `upload_max_bytes`.

## Tests (TST-1)

| Test | Asserts |
|---|---|
| `frontend` vitest on `ChatComposer` | busy + `onStop` renders Stop and calls it; not busy renders Send; Enter while busy does not stop |
| `conversation.test.tsx` (updated) | the existing Stop tests (`:531`, `:685`) pass against the composer button; chips add, show an error and remove |
| pytest: upload route | 404 for a thread the user cannot see; refusal shape equals `send`'s; a non-multipart body is 400; an oversize body is 413; a traversal name lands as a basename |
| pytest: send with `upload_ids` | an id from another thread or another user is refused; >10 is refused; the ids land on the message row |
| pytest: `complete_pending` | the prompt carries the `Attached files:` block with one line per upload; the channel branch is unchanged |
| pytest: run preparation | a staged dir lands at `shared/attachments/<message_id>/` with the thread gid, the staging copy still exists afterwards, and a second run of the same job is a no-op |
| pytest: `publish_job_on` | `attachments` round-trips to the `job` row and defaults to NULL |
| pytest: sweep | an orphan older than the retention goes; one referenced by an un-run message stays |

## Docs (DOC-1)

- `docs/modules/conversation.md` (or the panel doc that owns the composer): the `+`, drag&drop,
  the limits, where the file lands, what the agent sees.
- `docs/config/`: the three new `conversation/limits/*` paths.
- `docs/development/frontend.md`: `ChatComposer`'s new props.
- `DECISIONS.md` (DOC-2): one entry covering the three non-obvious choices — the attachment
  dir is keyed by `message_id`, not `job_id` (the job id does not exist when the prompt is
  written); `web` writes a staging dir instead of the thread tree (`web` holds no `THREAD_GID`
  and the thread dir may not exist yet); run preparation **copies** rather than moves (the
  runner has no `DAC_OVERRIDE`).
- `docs/architecture/` (`threads.md`, `runner.md`): `shared/attachments/` is a new thing in the
  thread tree, and run preparation gained a step.

## Settled

Quoted from the owner, 2026-11-18 (grilling rounds 1-4, "Zgadzam się ze wszystkimi punktami",
"agree to all points", "agreed"):

1. A file is always an attachment of a message; no job starts from a file alone.
2. The file lands under the thread's `shared/attachments/`, one dir per turn. **Changed from
   the settled text** (Q2/Q17 said `<job_id>`): the job id does not exist when the prompt is
   written (Verified facts), so the key is `<message_id>`, which is per-turn as agreed. Needs
   the owner's nod (PLN-4).
3. The agent learns about it from a line in the prompt; no new tool.
4. `web` writes it — "the file comes from the trusted panel side, not from the agent".
5. 25 MB/file, 10 files/message, no type allow-list, removed with the thread.
6. Panel only; Outlook/Jira attachments unchanged.
7. `web` writes a staging dir; run preparation moves it while still root.
8. A dedicated multipart route, not base64-in-JSON.
9. The file uploads on drop; `POST /messages` carries `upload_ids`.
10. A cron sweep removes orphans.
11. Names appear in the message text; no attachment table this round.
12. The Stop button moves into the composer as the send button's busy state.
13. The status text above stays; only the button moves.
14. The textarea stays typeable while a turn runs.
15. The whole conversation pane is the drop target, with a dashed overlay.
16. The `Attached files:` block has the shape shown in B-6, with the `<message_id>` key of
    item 2.
17. No upload in the channel composer.
18. Upload is authorized against the thread, at upload time.
19. The sweep keys on "no message row", not on age alone.
20. One vitest + one pytest group; A ships as its own commit before B.
