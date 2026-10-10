# Frontend: panel, UI kit and miniapp kit (E8)

The panel is a static React app. `proxy` serves its built files on the panel origin; it calls
only the `web` API that E2, E3 and E6 already ship. The miniapp kit is one stylesheet and a few
custom elements that a miniapp page loads from `/_ui/<version>/` on the apps origin. Both get
their look from the Mantine theme (`packages/ui/src/theme.ts`): the panel through
`AgentoUiProvider`, the kit through `agento-ui.css`, which the build generates from the theme. So
a panel button and a miniapp button look the same.

## Layout

| Path | What it is |
|---|---|
| `frontend/packages/miniapp-kit/` | The `--ag-*` variables resolved from the theme (`scripts/tokens.ts`), the `.ag-*` rules (`src/components.css`), the custom elements (`src/agento-ui.js`), and the build of `agento-ui.css` |
| `frontend/packages/ui/` (`@agento/ui`) | The Mantine theme and `AgentoUiProvider`, React components (Mantine or `.ag-*`), and the stories |
| `frontend/lookbook/` | The ui.mantine.dev patterns, copied as a Storybook catalogue (see [Lookbook](#lookbook)) |
| `frontend/packages/api/` (`@agento/api`) | `apiFetch`, the session, the query client, the stream hub, the panel module contract |
| `frontend/panel/` | The app: shell, router, login, users and roles, miniapp launches, the admin screens (`src/routes/admin/`) |
| `src/agento/modules/<core module>/panel/` | A core module's screens (today: `conversation`) |
| `frontend/miniapps/examples/` | Hand-written example miniapps |

Built output (gitignored, shipped in the wheel):

- `src/agento/framework/web/panel/` — mounted read-only into `proxy` at `/srv/panel`.
- `src/agento/framework/web/miniapp-ui/<version>/` — mounted at `/srv/miniapp-ui`, served as `/_ui/<version>/`.
- `src/agento/modules/miniapps/skills/miniapp-ui/SKILL.md` — the generated authoring skill (committed).

## Commands

Node 22.6 or newer. From `frontend/`:

```bash
npm ci
npm run build       # kit + skill + module registry + typecheck + vite build
npm run dev         # gen once, then rebuild the panel on every source change; reload the browser
npm run gen         # rerun after editing the kit (theme.ts, components.css, agento-ui.js, catalogue)
                    # or adding a module panel/index.ts — `dev` does not watch the generators
npm run storybook   # component workbench on :6006
npm test            # build, lint, then every unit test and every story in headless Chromium
```

`bin/test` runs `npm test` as step 9. In the dev stack the build is live: `proxy` mounts
`src/agento/framework/web/` directly, so a rebuild needs no container restart.

## Rules

- **A component gets its look from the theme only.** `Button`, `StatusBadge`, `TextField`,
  `SelectField` and `DataTable` render Mantine components; the cards, headers, states and code
  blocks use `.ag-*` classes. ESLint refuses a `style` or `styles` prop in `packages/ui` (stories
  excepted): a per-component override is a value the kit cannot follow. Change a value in
  `theme.ts`, and mirror a component change in `components.css` — the parity story fails until
  both agree. Every `MantineProvider` is `AgentoUiProvider`, so the AA overrides always apply.
  `SelectField` is a Mantine `Select` (combobox), so its open list has the theme's look. A miniapp
  writes `<select class="ag-field__input">`: the closed box and chevron are the same (the parity
  story checks them), and the browser draws its open list. `contained` on `TextField` and
  `SelectField` puts the label inside the box, as `.ag-field--contained` does. The shell nav
  follows the `NavbarSimple` pattern and the user list follows `UsersRolesTable`.
- **A date goes through `Timestamp`.** It formats with `formatTimestamp` (`us`, `eu` or `iso`,
  in a fixed `en-US` locale and the configured zone) from the `DisplayFormatProvider` that the
  panel fills from the session's `display` ([admin.md](../modules/admin.md)). With no provider it
  shows `toLocaleString()`. Do not call `toLocaleString` or `Intl.DateTimeFormat` in a screen.
- **An action is an icon with a title (UI-7).** A row, header or copy action is `IconAction`
  (`ArchiveAction`, `CopyButton`): a lucide icon in a Mantine `ActionIcon` inside a `Tooltip`, its
  name as the title and the `aria-label` (tests find it by name, as before), `danger` for a
  destructive one. A form submit, a dialog footer, a state's Retry and a page's create action stay
  text `Button`s.
- **A `DataTable` cell is a plain function, called on each render.** Do not render it as a component:
  a new component type per render remounts the cell, and an open select in it closes.
- **No credential in the UI.** The CSRF token lives in a module closure in `session.ts`: never in
  storage, a cookie the page can read, a URL or a log. A write with no token is not sent. The
  launch exchange code is used once, in a hidden form POST, and is never in a URL, a cache, React
  state or the window name.
- **A conversation is one timeline store.** `timeline.ts` keys every event by id: the newest
  timeline page, an older page ("Load older") and each stream frame merge into it, so an event seen
  twice renders once. The hub opens the stream with `?after=<newest id of the page>`, so there is no
  window between them; a stream frame that changes a message or a run also refetches the messages
  and the runs over REST (debounced). The turn state and Unblock still come from the messages. When
  the stream is down, the REST poll (10 s while a run is in flight, 30 s idle) is the fallback:
  each tick pages the timeline forward through `GET …/events?after=<cursor>` and refetches the
  runs. The cursor is the highest id up to which every event is held: only replay pages (or the
  newest page a 409 reloads) move it, never a stream frame or a reconnect page. Messages whose
  events retention pruned show as plain bubbles before the timeline. Every merge has an origin:
  `older` ("Load older") never trims; `auto` (stream, replay, reconnect, newest page) trims the
  store to the newest 5000 while the page follows live output. Scrolled up, `auto` events are not
  held, only counted in "N new events", and replay paging stops; following again resets the store
  to the newest page and the replay cursor to its newest id, the same reset a 409 does. A reload
  whose page arrives after the operator scrolled up again changes nothing. The hub listens for each event kind by name: a new kind goes
  into `STREAM_KINDS` in `hub.ts`, or the browser drops it.
- **Agent text is Markdown.** `Markdown` (`react-markdown` + `remark-gfm`, loaded lazily so the
  entry chunk stays in budget) renders it in Mantine's `Typography`; raw HTML is shown as text, and
  links open with `rel="noopener noreferrer"`. Its kit twin is `.ag-prose` (kit 1.1.0): the kit does
  not parse Markdown, the miniapp writes the HTML. `<Markdown highlight>` is opt-in: a fenced block
  renders with `@mantine/code-highlight` and the `highlight.js` common languages (language label,
  copy button), both in their own lazy chunk. The kit has no syntax highlighting, so only the chat
  passes `highlight`; the default `Markdown` stays what the parity story compares with `.ag-prose`.
  (The `@catalogue` text of `.ag-prose` is part of the released kit bytes, UI-3: it says so only
  when the next kit version is cut.)
- **The chat is panel-only.** `ChatLayout`, `ChatList`, `ChatMessage`, `ToolCall`, `ToolGroup`,
  `Reasoning`, `ChatStatus`, `ChatError`, `ChatComposer`, `RunInfo`, `SplitView`, `ThreadList` and
  `MenuButton` (`packages/ui/src/components/Chat*.tsx`) are Mantine components with no `.ag-*`
  twin: a miniapp shows no chat (UI-2). The timeline fills the shell's main area; below `sm` the
  thread list is a `Drawer`. Each rendered timeline item is exactly one `li` of `ChatList`.
- **A conversation shows turns, not runs.** `buildItems` keeps the partial segments of a run
  (`assistant.partial` / `reasoning.partial` grow one live item; the next complete
  `assistant.text` / `assistant.reasoning` replaces it in place, B1). `turns()` merges the runs
  (attempts) of one job into one turn: it shows the newest attempt's items, two or more tools in a
  row fold into "Used N tools", and the errors fold into one failure — "Retrying (attempt n of m)"
  while attempts are left (`run.started.max_attempts`, `job.failed.attempt`), else one "The agent
  could not answer." with Retry, which posts the turn's question again as a new message. One
  status line under the newest turn says Queued / Starting / Thinking / Running `<tool>`. A tool
  call is one line from `tools.ts` (`toolSummary`): the verb and its argument, or the verb alone
  for a reader without `conversation.run_details` (the server strips the input). Run details
  (`RunInfo`: status, attempt, harness · provider, credential, model, tokens, job) show only when
  the thread's `run_details` is true; the job link only for an admin. A
  channel run starts with an incoming message "From `<source>` · `<reference>`"; its trigger text
  only when the server sent `prompt`. The composer always takes typing; only Send waits.
- **One teardown.** Logout, the first 401 and a new login call `endSession()`: it clears the query
  cache, closes the stream and closes every launch window.

## Admin screens

The admin TUI's screens are in the panel under the **Administration** group, for an admin only:
Dashboard (`/`), Jobs, Agents, Credentials, Tools, Skills and Config (`/admin/<name>`), plus Users.
Each is a lazy route in `panel/src/routes/admin/`, so the entry chunk stays inside its budget. They
call `/api/admin/*` ([panel.md](../architecture/panel.md#admin-screens)).

- **The scope is in the URL.** `ScopePicker` (Default / Workspace / Agent view) writes
  `?scope=&scope_id=`, so a reload or a link from Agents ("Open config") keeps it. Tools, Skills
  and Config use it.
- **A secret is never shown.** A secret config row shows `Set` or `Not set` and the editor is
  disabled with the CLI hint. A credential shows that it has an error, never the message.
- **What the panel cannot do, it names.** Replay and build show the CLI command with a copy
  button ([docs/cli/admin.md](../cli/admin.md)).
- **ENV overrides are not shown.** `CONFIG__*` ENV overrides are not visible on the Config screen;
  `bin/agento config:resolve <path>` shows the effective value. The module list shows a title made
  from the key (`agent_view` → "Agent View"); the URL keeps the key.
- **Access and enablement are the same tree.** Role resources, Tools and Skills all render
  `routes/ResourceTree.tsx`: a Mantine `Tree` of checkboxes with search, expand/collapse, select
  all/clear, and the sticky `UnsavedBar` (Reset and Save as icons, UI-7). The page gives it one
  memoized `toData(search)`; the tree owns `search` and the expanded state, so a page that keys it on
  its scope clears both on a scope change. `useTree` holds only the expanded state: the checked
  leaves are a `Set` the page holds (`roleTree.ts`, `enablementTree.ts`), because `useTree`'s own
  checked state drops the leaves a search hides. **Expand all** is shown only while a group is
  closed and **Collapse all** only while one is open, over the data a search leaves visible.
- **A role's access.** Users → Roles lists the roles; a role page (`/users/roles/<code>`) has Role
  info and Role resources. Role resources picks one workspace or agent view by name, then shows the
  operations and the tools of that place, saved with one `PUT`. A leaf inherited from the workspace
  or built in for admin is checked and locked, and is never sent. A tool that is off at that place is
  marked "Off here", because a grant does not enable a tool.
- **Tools and Skills save on demand.** A checkbox edits a draft; **Save** sends only the changed
  gates as one batch of `PUT /api/admin/config` and the draft clears once that screen's own list has
  refetched. Drafts are held **per scope** (`routes/admin/EnablementTree.tsx`), because the scope is
  in the URL and the screen stays mounted across a change of it — see DECISIONS.md. A failed save
  keeps the draft, so the write can be retried.
- **Mouse and keyboard.** `DataTable` activates a row on Enter; each admin table also has a button
  column (Details, Usage, Edit) for the mouse.

## Adding a module screen

Only a **core** module (`src/agento/modules/<name>/`) may ship panel code. A user or PyPI module
never ships JavaScript into the panel origin.

```ts
// src/agento/modules/<name>/panel/index.ts
import type { PanelModule } from "@agento/api";

const panel: PanelModule = {
  contractVersion: 1,
  id: "<name>",                                   // the module directory
  availability: { probe: "/api/<name>/<a GET route the module owns>" },
  routes: [{ path: "/<name>", nav: "<Label>", load: () => import("./Page.tsx") }],
};
export default panel;
```

`npm run gen` collects every `modules/*/panel/index.ts` into the registry and stops the build with
a `RegistryContractError` when a module breaks the contract. The panel shows the screen only when
the probe answers 2xx; a disabled module's routes answer 404, so it fails closed. A module panel may
import `react`, `react-router`, `@agento/ui`, `@agento/api` and its own files — a test checks the
import graph.

## The miniapp kit

A miniapp is hand-written HTML. It links `/_ui/<version>/agento-ui.css`, loads
`/_ui/<version>/agento-ui.js` (the `<ag-table>`, `<ag-dialog>`, `<ag-copy>`, `<ag-json>` elements),
and imports `createAgentoSdk` from `/_ui/<version>/agento-bridge.js` (a byte copy of the E6 SDK).
The agent-facing guide is the `miniapp-ui` skill, generated from the `@catalogue` comments in
`components.css`, with the `name`/`description` frontmatter every module skill carries (Codex does
not load a skill without it); enable it per scope like any skill. The build steps and limits are in
the hand-written `miniapp-build` skill ([../modules/miniapps.md](../modules/miniapps.md#writing-a-miniapp)).

A released kit version never changes: its URL is served `immutable`. The released bytes are
committed under `frontend/packages/miniapp-kit/released/<version>/`, and the build fails when the
current build of a released version differs. To change the kit, bump `version` in
`packages/miniapp-kit/package.json`, then run
`node --experimental-strip-types packages/miniapp-kit/scripts/build-kit.ts --release`. Old
versions stay served, so existing miniapps keep their look. A change to `theme.ts` or a Mantine
upgrade changes the generated CSS too, so it also needs a new kit version.

## Lookbook

`frontend/lookbook/` holds the ui.mantine.dev patterns (MIT, `LICENCE`), copied at commit
`7bce6d7`, one story file per category under `Lookbook/` in `npm run storybook`. It is a
catalogue to copy from, not a package: nothing imports it, lint skips it, and its stories are
tagged `!test` (third-party code with remote images). Its CSS modules need
`postcss-preset-mantine`, which only Storybook loads. The drag-and-drop, carousel and dropzone
patterns need `@dnd-kit/*`, `@mantine/carousel` (with `embla-carousel`) and `@mantine/dropzone`:
devDependencies only, so none of them reaches the panel bundle. To use a pattern in the panel, move it into
`@agento/ui` and add its `.ag-*` twin when a miniapp needs it too.

## Tests

| What | Where |
|---|---|
| CSRF in memory only, A → logout → B leaves nothing, a stale response is dropped | `packages/api/src/api.test.ts` |
| Stream reconnect, refused stream, `cursor_expired`, backoff, `?after=` | `packages/api/src/hub.test.ts` |
| Popup blocked, API refusal closes the window, bridge before POST, code in no URL or cache | `panel/src/launch.test.ts` |
| Roles list, Delete kept for a built-in or used role, Add role; the role tree saves one PUT, locked leaves are never sent, a search keeps hidden checks, a scope switch asks before it discards | `panel/src/routes/Roles.test.tsx`, `panel/src/routes/RoleEdit.test.tsx`, `panel/src/routes/roleTree.test.ts` |
| Probe 404 / 2xx / network error → nav and NotAvailable | `panel/src/registry.test.tsx` |
| The example miniapp decodes the toolbox action envelope | `packages/miniapp-kit/kit.test.ts` |
| Dialog focus trap and focus return | `packages/ui/src/components/Forms.stories.tsx` (`DialogFocus`) |
| Stream opens after the page's newest id, persisted events refetch and never write the cache, 10 s / 30 s reconcile polls, follow-live and "N new events", reset to the newest page on resume, older-page anchor, read-only channel thread | `modules/conversation/panel/conversation.test.tsx` |
| Dedupe by id, tool pairing, runs grouped by execution, the final-answer rule over split pages, partial segments, tool groups, attempts folded into one turn | `modules/conversation/panel/timeline.test.ts` |
| Queued / Running status line, live partial text, one failure with Retry, typing while busy | `modules/conversation/panel/conversation.test.tsx` |
| Tool summary per tool and without input, durations; thread day groups | `modules/conversation/panel/tools.test.ts`, `modules/conversation/panel/model.test.ts` |
| `Markdown` renders a code fence and a table; raw HTML stays text; opt-in highlight; composer keys, tool row, Retry, thread list, menu button | `packages/ui/src/components/components.test.tsx` |
| WCAG AA contrast, catalogue completeness, skill drift, kit immutability | `packages/miniapp-kit/kit.test.ts` |
| The three date formats in a fixed zone, the provider, `Timestamp` with no provider | `packages/ui/src/components/Timestamp.test.tsx`, `panel/src/DisplayFormat.test.tsx` |
| Import boundaries, bundle budget | `boundaries.test.ts` |
| Every story renders with no a11y violation; panel card = miniapp card in light and dark | `packages/ui/src/**/*.stories.tsx` |
| Each Mantine component = its `.ag-*` twin (computed styles and layout), light and dark | `packages/ui/src/acceptance/Parity.stories.tsx` |
| The wheel ships the panel and the kit (runs after the build) | `tests/check_wheel_frontend.py` |
| Proxy routes and cache headers | `tests/unit/framework/docker/test_proxy_config.py`, `docker/smoke/proxy-smoke.sh` |
