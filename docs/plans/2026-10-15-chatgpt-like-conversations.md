# A ChatGPT-like conversation screen

Status: approved by the owner on 2026-10-15 (see Settled). Branch `feature/runtime-split`.

## Goal

The conversation screen — the list, the timeline, the composer — reads like a modern chat app:
neutral grays, soft radii, one-line thread rows with hover actions, a pill composer. The list
gains the three things it lacks today: **pin**, **rename** and **search**.

## Verified facts (PLN-1)

| Fact | Proof |
|---|---|
| The chat CSS is panel-only and has no `.ag-*` twin, so UI-2 does not bind it | `frontend/packages/ui/src/components/Chat.module.css:1` |
| `theme.ts` is resolved into the kit's `agento-ui.css` at build time | `frontend/packages/miniapp-kit/scripts/tokens.ts:1-16` |
| Kit `1.1.0` is released and frozen: a byte change in the built CSS fails the build | `frontend/packages/miniapp-kit/scripts/build-kit.ts:68,73`; `released/1.0.0`, `released/1.1.0` |
| So a `theme.ts` change needs a kit version bump (UI-3) | same |
| The rail today: `PageHeader` + full-width `MenuButton` + admin `SelectField` + `ThreadList` | `src/agento/modules/conversation/panel/ConversationsPage.tsx:26-70` |
| A thread row is a Mantine `NavLink` with `label` **and** `description` | `frontend/packages/ui/src/components/ChatShell.tsx` (`ThreadList`) |
| There is no pin, no rename and no search in the module — schema, routes or panel | `grep -rin "pin\|search\|rename" src/agento/modules/conversation` (2026-10-15): only unrelated prose |
| `conversation` has `title VARCHAR(255) NULL`, `status`, `updated_at` | `src/agento/modules/conversation/sql/001_conversation.sql:5-23` |
| `service.check_title` already validates a title (≤255, utf8) | `src/agento/modules/conversation/src/service.py:632-638` |
| The owner list is `WHERE c.user_id = %s AND c.status='active' ORDER BY c.updated_at DESC, c.id DESC` | `src/agento/modules/conversation/src/service.py:148-149` |
| A thread auto-archives on its newest-message clock (`_IDLE`) | `src/agento/modules/conversation/src/retention.py:131-146` |
| Deletion is deliberately an operator path; the route archives | `src/agento/modules/conversation/src/routes.py:121-122` |
| `RowMenu` (3-dots row actions) already exists, panel-only | `frontend/packages/ui/src/components/RowMenu.tsx` |
| Routes are declared in `di.json`; `PATCH` must be added there | `src/agento/modules/conversation/di.json:34-97` |

## Scope

### In — Tier A: the look (no backend)

1. **Theme (`packages/ui/src/theme.ts`), kit `1.1.0 → 1.2.0`.** Near-neutral gray and dark
   scales (ChatGPT's grays carry almost no hue), `defaultRadius: "lg"`. Every raised color in
   `agentoCssVariables` is re-checked for WCAG AA in `kit.test.ts` (UI-1). The kit is rebuilt
   under `released/1.2.0`; nothing edits `released/` by hand (UI-3).
2. **The rail** (`ChatShell.tsx`, `Chat.module.css`, `ConversationsPage.tsx`):
   - the `PageHeader` goes; the top row is a quiet **New conversation** row (pen icon, keeps
     the `MenuButton` view pick) and a **search** `IconAction`;
   - search toggles a `TextInput` that filters the loaded list by title;
   - a thread row is **one line**: title only, radius `lg`, flat gray hover and active, no
     description — channel and agent_view move into the row's `title` attribute;
   - the admin scope `SelectField` loses its label and sits next to search;
   - group labels (`Today`, …) are small, muted, with air above; a **Pinned** group comes first.
3. **The timeline** (`Chat.tsx`, `Chat.module.css`):
   - the user bubble is `radius xl` with more horizontal padding, max-width 70%;
   - `RegenerateAction` moves into the per-message hover action row (copy · retry), not its
     own row under the thread;
   - the header actions (`ConnectionStatus`, `RunInfo`, `ArchiveAction`) fold into one `…`
     menu on the right; the connection state shows only when it is not `live`;
   - the composer is one bordered pill (`Paper radius xl`) holding the textarea, with a round
     filled send button inside it, and one muted disclaimer line under it.

### In — Tier B: pin, rename, search

4. **`sql/006_conversation_pin.sql`**: `ALTER TABLE conversation ADD COLUMN pinned_at TIMESTAMP NULL`,
   and `KEY idx_user_pinned (user_id, status, pinned_at)`.
5. **`PATCH /api/conversation/threads/{id}`** — `{"title"?: string|null, "pinned"?: bool}`, the same
   `load_visible` gate as every other handler (404, never 403). `title` reuses `check_title`;
   `pinned` writes `pinned_at = NOW()` or `NULL`. Response: the thread JSON.
   `_conversation_json` gains `"pinned": bool`. Declared in `di.json`.
6. **Order, and why pins cannot starve the list**: the owner list becomes
   `ORDER BY (c.pinned_at IS NULL), c.pinned_at DESC, c.updated_at DESC, c.id DESC`. The owner
   list has **no pagination** (`before` is channels-only, `routes.py:93-96`) and one `LIMIT`
   (`service.py:151`), so pinned-first alone would hide every new thread once the pin count
   reached `history/page_size`. Two bounds make that unreachable:
   - `MAX_PINNED = 50` per user. `PATCH {"pinned": true}` beyond it is `409` with
     `"at most 50 pinned conversations"`. The cap is counted and written **under one per-user
     lock**, in one transaction: `SELECT id FROM user WHERE id = %s FOR UPDATE` first, then the
     count, then the `UPDATE`. The thread's own row lock cannot hold this bound — two pins of
     *different* threads lock different rows, so both can read 49 and both commit. **Lock order**
     (SCL-1): user row, then conversation row; no other path takes the user row lock, so no cycle
     with the posting path, which takes the conversation row only.
   - the owner query asks for `LIMIT history/page_size + MAX_PINNED`, so **at least
     `history/page_size` unpinned rows are always returned**, whatever the pin count.
   The channel list is unchanged (a channel thread has no owner to pin it).
7. **Retention**: a pinned thread is exempt from the idle auto-archive — `_IDLE` gains
   `c.pinned_at IS NULL`. Pinning a thread that then silently archives is a surprise.
8. **Row menu**: Rename · Pin/Unpin · Archive, through `RowMenu`. Rename is a prompt-free inline
   edit in the row.

### Out

- **Delete.** `routes.py` states deletion is an operator path with its own ordering (§10.1). Archive stays the panel action.
- **Server-side search.** The filter is client-side over the loaded page (`history/page_size`),
  marked with a `ponytail:` comment naming the ceiling and the upgrade (`?q=` LIKE on title).
- **Merging the two sidebars.** The AppShell navbar and the 56px header stay on every screen.
- Attachment, voice and model-picker controls in the composer: nothing behind them.

## Placement (PLC-1)

| Unit of logic | Where it runs | Who owns it | The question that decided it |
|---|---|---|---|
| `pinned_at` column, the `PATCH` handler, `check_title` reuse, the pin cap | Python | the `conversation` core module | PLC-1 q1: no tool, no tool credential → Python. q7: it has its own tables and routes → its own core module, which already owns them |
| The pinned-first ordering in `list_visible` | Python | `conversation` | same row; it is one `ORDER BY` in the query that already lives there |
| The `pinned_at IS NULL` term in `_IDLE` | Python | `conversation` | same row; `retention.py` is this module's own cron path |
| The rail, the row menu, the search filter, the pin control | the browser | the panel (`conversation/panel/`) | a module's own screen lives in its `panel/` directory; it may not import Mantine (`boundaries.test.ts`) |
| `ThreadList`, `SplitView`, `ChatMessage`, `ChatComposer`, `RowMenu` changes | the browser | `@agento/ui` | they are shared components the panel composes; the chat ones are panel-only and have no `.ag-*` twin (`Chat.module.css:1`) |
| The gray scale and `defaultRadius` | build time (`tokens.ts`) and the browser | `packages/ui/src/theme.ts` | UI-1: a color or radius value has exactly one source, and the kit is generated from it |

No new framework code, no new module, no Node and no toolbox work: nothing here touches a tool
credential (SEC-1 q1 is "no" for every row).

## Extension points (EVT-1)

- **Pin / unpin** (`pinned_at` set or cleared) — `no event: ` it is one user's own view preference on
  their own thread. No in-tree module and no `app/code` use case can act on "this user pinned a
  thread" that it could not get from the row, and an event per toggle is noise on a control a user
  clicks repeatedly.
- **Rename** (`title` set by the user) — `no event: ` the title is already written by the system on
  the first message (`service.py:668-671`) with no event, and EVT-2 requires every path of a
  transition to agree: adding an event to the manual path only would make the family inconsistent,
  and adding it to both would fire on every first message in the system.
- **A pinned thread is exempt from the idle auto-archive** — `no event: ` this is not a new hold that
  silently stops work. `archive_after` (the existing event on `service.archive`) still fires for
  every thread that *is* archived; a pinned thread simply never reaches it, and the reason is on
  screen — the user set the pin and sees the pin icon on the row. Nothing waits on an archive that
  does not come.

## Tests (TST-1)

- `tests/.../conversation`: PATCH renames, pins, unpins; a non-visible thread is 404; a bad title is 400;
  the 51st pin is 409; **concurrency: from 49 pins, two pins of different threads race — exactly one
  commits, the other is 409, and the final count is 50**; the owner list puts a pinned thread first; **with `MAX_PINNED` pinned threads
  the list still returns `history/page_size` unpinned rows, the newest among them** (the starvation
  case); `auto_archive` skips a pinned thread.
- `conversation.test.tsx`: the row menu pins and renames; search filters the list; the Pinned group shows.
- `kit.test.ts`: contrast pairs under the new scale.

## Docs (DOC)

- `docs/modules/conversation.md` — pin, rename, search, the `PATCH` route, `MAX_PINNED`, the
  retention exemption.
- `docs/development/frontend.md` — **required, not conditional**: it names kit `1.1.0` at line 92 and
  promises classes "in the next kit version" at lines 98 and 108, all of which this bump makes stale.
- The `@catalogue` block of every `.ag-*` class whose look the theme change alters (it regenerates
  the `miniapp-ui` skill, DOC-1).
- `DECISIONS.md` — the pin cap and why the owner list is bounded rather than paginated (DOC-2).

## Settled (PLN-4)

- "Keep both, restyle only" — owner, 2026-10-15 (the AppShell navbar and the conversation rail both stay).
- "Tune theme.ts + bump kit to 1.2.0" — owner, 2026-10-15 (the gray scale and radius change globally).
- "Both tiers in one go" — owner, 2026-10-15 (the restyle and pin/rename/search in one change).
- "maybe just keep the gray background" — owner, 2026-10-15 (neutral grays, not a new accent hue).
