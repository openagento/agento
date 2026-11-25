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
| `src/agento/modules/<core module>/panel/` | A core module's screens (today: `conversation`, `miniapps`, `scheduler`, `versioned_artifacts`) |
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
  `theme.ts` sets its own near-neutral `gray` and `dark` scales and `defaultRadius: "lg"` (kit
  1.2.0). A default radius is a theme value, so the kit emits it as `--ag-radius-default`, and the
  `.ag-button` / `.ag-field__input` twins use that token — not a hardcoded step — or the parity
  story fails the next time the default moves.
- **One scrollbar look (kit 1.4.0).** A list or a thread scrolls in a Mantine `ScrollArea`
  (`theme.ts` sets `scrollbarSize: 8` for all of them; do not set it per call). Every other scroll
  box (a textarea, a JSON input, a modal body, a wide table, the page) gets the browser's
  scrollbar, which `agento-ui.css` makes thin, with the `ScrollArea` thumb
  (`--ag-color-scrollbar-thumb`) on no track. `data-theme` sets `color-scheme` too, so the browser
  draws that scrollbar dark when the panel is dark, also when the OS is light. Do not style a
  scrollbar in one component: change the kit rule (UI-9).
  `SelectField` is a Mantine `Select` (combobox), so its open list has the theme's look. A miniapp
  writes `<select class="ag-field__input">`: the closed box and chevron are the same (the parity
  story checks them), and the browser draws its open list. `contained` on `TextField` and
  `SelectField` puts the label inside the box, as `.ag-field--contained` does. The shell nav
  follows the `NavbarSimple` pattern and the user list follows `UsersRolesTable`.
- **A date goes through `Timestamp`.** It formats with `formatTimestamp` (`us`, `eu` or `iso`,
  in a fixed `en-US` locale and the configured zone) from the `DisplayFormatProvider` that the
  panel fills from the session's `display` ([admin.md](../modules/admin.md)). A format an operator
  set (`date_format_explicit`) always wins. With none set, English keeps `us` and any other panel
  language writes the date its own way (`Intl`, in that language); the zone never depends on the
  language. With no provider (a component outside
  the panel, a test) it writes the `us` format in the browser's zone; the panel always gives one, also when `display`
  is `null` (`admin` disabled): then the format follows the panel language and the zone is the
  browser's. The configured zone is the server's choice, and the zone data of a browser can
  lack it: `Timestamp` then writes the moment in UTC and the label ends with `UTC`. It never
  writes the browser's time as if it were the configured zone. A wall time that the server
  computed in a zone of its own (a next run of a schedule) is `<WallTime local={f.local}
  at={f.at} />`: the label is the server's `local` in the panel's format and language, with no
  zone conversion in the browser, and `at` is the machine instant. Do not call `toLocaleString` or
  `Intl.DateTimeFormat` in a screen.
- **Only `packages/ui/src/intl.ts` calls `Intl`.** An installed language can have a tag that the
  browser has no data for (`qaa`). `Intl` then writes in the default language of the browser, and
  so does a `toLocale…` call with no tag: two users of one panel would read two formats. The
  module gives each formatter `formatLocales(tag)` (the tag, then English). When you need a
  formatter that `useFormat` and `Timestamp` do not give, use `numberFormat`, `dateTimeFormat`,
  `relativeTimeFormat` or `timeZones` from `@agento/ui`. `numbers.test.ts` fails on an `Intl`
  call, a `toLocale…String` call, `toLocaleUpperCase`, `toLocaleLowerCase` or `localeCompare` in
  any other module of the panel. A tag that is not well-formed would make `Intl` throw; no
  such tag comes in, because the API boundary refuses an answer whose tag does not match the
  server's `TAG_RE` (see "A 2xx answer is checked" below).
- **A text a user reads comes from a catalog.** See [Languages](#languages).
- **An action is an icon with a title (UI-7).** A row, header or copy action is `IconAction`
  (`ArchiveAction`, `RegenerateAction` — re-ask the turn above, in a chat timeline — and
  `CopyButton`): a lucide icon in a Mantine `ActionIcon` inside a `Tooltip`, its
  name as the title and the `aria-label` (tests find it by name, as before), `danger` for a
  destructive one. A form submit, a dialog footer, a state's Retry and a page's create action stay
  text `Button`s.
- **React refreshes the data, not the user (UI-8).** Data that a backend process changes is read
  again by its query: a `refetchInterval` that matches the writer (the Credentials page reads every
  minute, as `credential:limits` runs), or an `invalidateQueries` after the action. No page reload. Data that only the user on the page
  changes (settings, config, users, roles, tool grants, app settings) has no poll: it refreshes after
  its own save.
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
  and the runs over REST, at most once per 2 s (`REFETCH_MS`: one tab on a run with sub-agents
  otherwise passes `limits/max_requests_per_user`); the list keeps its own 10 s poll. The turn state and Unblock still come from the messages. When
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
  links open with `rel="noopener noreferrer"`. Its kit twin is `.ag-prose` (kit 1.2.0): the kit does
  not parse Markdown, the miniapp writes the HTML. `<Markdown highlight>` is opt-in: a fenced block
  renders with `@mantine/code-highlight` and the `highlight.js` common languages (language label,
  copy button), both in their own lazy chunk, and a table goes in its own scroll box (a focusable
  `region` named "Table"). The kit has no syntax highlighting, so only the chat
  passes `highlight`; the default `Markdown` stays what the parity story compares with `.ag-prose`.
  (The `@catalogue` text of `.ag-prose` is part of the released kit bytes, UI-3: it says so only
  when a kit version is cut — it is in 1.2.0.)
- **Every in-panel link is `AppLink`.** `AppLink` (`packages/ui/src/components/Link.tsx`) is Mantine's
  `Anchor` rendered as a router `Link`, so a link takes the theme's anchor color
  (`--mantine-color-anchor`: `#1971c2` light, `#4dabf7` dark — the same value the kit gives a miniapp as
  `--ag-color-link`). A bare `Link` from `react-router` renders in the browser default (visited purple and
  all), so `boundaries.test.ts` refuses that import in `panel/src` and in every module panel; the one
  allowed definition site is `Link.tsx`. `Shell.tsx` keeps Mantine's `NavLink` for the navigation.
- **A row's actions can live behind 3 dots.** `RowMenu` takes `{ label, onClick, danger?, disabled?, icon? }[]`
  and shows them under a vertical 3-dots `IconAction` named "Actions" (UI-7); it renders nothing when it
  has neither an action nor a detail. `details` adds read-only `{label, value}` rows above the actions,
  so a header that would otherwise grow a row of icons (run details, archive) keeps ONE menu. A
  detail row is read-only: no pointer, no hover highlight — only an action looks clickable. `icon` is a
  NAME (`rename` | `pin` | `unpin` | `archive` | `reconnect`), not a node, because a module panel may not
  import lucide-react; `RowMenu` holds the mapping. Like the chat components it is **panel-only** with no `.ag-*` twin: a miniapp shows no admin
  row actions, and the kit has no menu class (ROADMAP: the `.ag-menu` twin is due in a later kit version
  if a miniapp ever needs a menu).
- **The chat is panel-only.** `ChatLayout`, `ChatList`, `ChatMessage`, `ToolCall`, `ToolGroup`,
  `SubagentRun`, `Reasoning`, `ChatStatus`, `ChatError`, `ChatComposer`, `SplitView`, `Rail`, `SearchToggle`, `ThreadList` and
  `MenuButton` (`packages/ui/src/components/Chat*.tsx`) are Mantine components with no `.ag-*`
  twin: a miniapp shows no chat (UI-2). The timeline fills the shell's main area; below `sm` the
  thread list is a `Drawer`. Each rendered timeline item is exactly one `li` of `ChatList`.
  The timeline scrolls only vertically (`scrollbars="y"`); an answer wraps long words and URLs,
  and its wide tables and code blocks scroll inside their own box (story `WideContent`).
  `ChatComposer` is one pill with one submit control: `stoppable` + `onStop` turn Send into
  Stop while a turn runs, and `attachments` + `onFiles` + `onRemoveAttachment` add the file
  row and the "Add files" button (a paste of files goes the same way). Both are optional, so
  the composer without them is exactly the field it was, and it holds no state of its own.
  The file pieces are exported for other panel forms, and the composer uses the same ones:
  `FileDrop` (a region that takes dropped FILES — a dragged link or text shows no hint — with the
  card over a scrim; `hint` is its second line), `AttachmentChips`, `AttachButton` (the paperclip
  and its hidden input) and `FilesField` (the three as one form field: label, hint, and an `empty`
  text). They are panel-only too: a file is staged through a session route of `web`, which a
  miniapp does not have. The state is `useUploads(endpoint, initial?)` of `@agento/api`: one POST
  of one file to `endpoint` per picked file; it returns `{files, add, remove, reset, ids, busy}`
  (`ids`: the staged ids in order; an `error` entry stays in `files` and is never in `ids`), and
  drops a late answer for an endpoint the user has left.
  The **drop** target is `ChatLayout`'s own `onFiles`, not the composer's: the whole
  conversation pane takes the drop and shows the hint over it, because during a drag the eye
  is on the history, and a target the user has to aim at is a target they miss. The page owns
  the staged list and hands the same one to both.
  A sub-agent's job is a `SubagentRun` card (collapsed, with its state badge) nested in its
  parent's turn; open, it holds its own `ChatList` ([conversation.md](../modules/conversation.md#turns-and-sub-agents-in-the-panel)).
  `Rail` is the thread column: a fixed top block (new conversation, the `SearchToggle`, the search
  box while search is open, and the scope picker: Mine and Shared with me for every reader, the
  channels for an admin) over a scrolling `ThreadList`. Shared with me is keyset paged: "Load more"
  reads the next page, and the poll refreshes every page loaded. A `ThreadLink` with `onPin` / `onRename` / `actions` shows a `RowMenu` on
  hover (always on touch); `ThreadList` groups the pinned threads above the day groups.
- **The app tiles are panel-only.** `ViewToggle` (Mantine `SegmentedControl`, "Tiles" / "List"),
  `TileGrid` (`SimpleGrid`, a `list`) and `AppTile` (`listitem`; icon name, title, `disabled`, badge,
  actions; two or more `choices` make it a menu) in `packages/ui/src/components/Tiles.tsx` have no
  `.ag-*` twin: a miniapp does not show them (UI-2). A disabled tile is `aria-disabled` with the
  title "You may view this app but not run it". The app icon is a **name**, never a URL:
  `APP_ICON_NAMES` maps a fixed set of lucide icons (a module panel may not import lucide-react),
  and the miniapps panel test keeps it equal to `miniapps/panel/icons.json`, which `web` checks.
  `IconPicker` (the icon as a button; a `Popover` with the set), `EditableTitle` (`Headers.tsx`; a
  pencil on hover, the title becomes an unstyled input, Enter or blur saves, Escape cancels) and
  `OpenMenuAction` (`IconAction.tsx`; the open icon, a `Menu` when there are two or more choices)
  are panel-only too. `SelectField` takes `searchable` and grouped `options`.
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
  (status, attempt, harness · provider, credential, model, tokens, job) are the `details` rows of
  the header's one `RowMenu`, and show only when the thread's `run_details` is true. Every header
  ACTION is in that same menu — Reconnect (a refused stream) and Archive; the connection badge
  beside it is status only, and shows only when the stream is not live. the job link only for an admin. A
  channel run starts with an incoming message "From `<source>` · `<reference>`"; its trigger text
  only when the server sent `prompt`. The composer always takes typing; only Send waits.
- **One teardown.** A confirmed logout, the first 401 and a new login call `endSession()`: it clears the query
  cache, closes the stream and closes every launch window. An answer can come back after the
  end: `apiFetch` drops it (`SessionChangedError`), but its caller runs some microtasks later.
  So a function of `@agento/api` that writes module state after an `await` takes the epoch
  before the request (`const at = epoch` / `getEpoch()`) and checks it in the same synchronous
  run as the write (`boot`, `saveLocale`, `logout`, `openLaunch`, `endLaunch`).
  `packages/api/src/lateAnswers.test.ts` scans the package for a write with no such check.
- **A logout ends the tab's session only when the server said so**: a 2xx, or a 401 (the session
  is already gone). With no answer, no network or a server error the server can still hold the
  session, so `logout()` rejects and the tab keeps its session; `Shell` shows
  `panel:shell.sign_out_failed`, stays on the page and lets the user press Sign out again (the
  link is disabled while one sign-out runs). When the session ended on another path during
  the request (the first 401 of another request), `Shell` does nothing: the expiry handler owns the
  navigation, so the address keeps `next` and the sign-in page shows no sign-out failure. The logout answer clears the session cookie, and
  the browser applies cookies in the order of arrival. So `login()` sends its request only after
  every pending logout request of this tab settled (an answer or a failure). The logout request
  has a deadline (`LOGOUT_DEADLINE_MS`, 5 s): `within` (the one deadline helper,
  `packages/api/src/deadline.ts`; the store's `withDeadline` uses it) aborts the request, so its
  late answer cannot arrive and clear the new cookie.
- **The language save has no deadline.** `languageSwitch.pending` holds until the PATCH settles,
  so one tab never has two saves in flight and a late commit of an old save cannot follow a newer
  one. Two tabs or two devices are last-write-wins on the server, as every account field is.
- **A 2xx answer is checked before it gets a type.** `apiFetch` gives the body as `unknown`
  (a body that is not JSON, or whose read was aborted, is `undefined`). The readers of the
  catalogs (`catalog.ts`) and of the session (`session.ts`: `boot`, `login`, `saveLocale`) give
  it a type only through `shaped(body, isX)`; an answer with another shape rejects with a
  `TypeError`, as a failed read does. The checks use the server's own rules: each language tag
  (the catalog `locale`; `effective`, `instance`, each `available` tag, `ui_locale`) matches
  `TAG_RE` (`isLanguageTag` in `shape.ts`), and each message of `messages` and `fallback` is a
  text or a plural object with `other`, plural categories as keys and texts as values
  (`isMessage` of `@agento/ui`, which `translate` uses too). `api.test.ts` reads `TAG_RE` from the
  Python source and compares it; `packages/ui/src/i18n.test.tsx` does the same for `PLURAL_CATEGORIES`. So an unreadable catalog shows the bundled English, an
  unreadable save answer is a failed language change, and an unreadable session answer starts no
  session. `packages/api/src/api.test.ts` fails on a new `apiFetch<T>` or `body as T` in the
  package; `jobLinks.tsx`, `launch.ts` and `useUploads.ts` are its listed exceptions.

## Languages

The panel shows one language per user. The texts live in flat JSON catalogs, one file per
namespace and language: `src/agento/framework/i18n/ui/<tag>.json` (the `@agento/ui` components),
`src/agento/framework/i18n/panel/<tag>.json` (the panel's own screens) and
`src/agento/modules/<module>/i18n/<tag>.json` (a module; the namespace is the module name).
`en.json` is the source. A message is a string with `{param}` holes, or a plural object
(`{"one": …, "other": …}`) that `count` selects.

| You need | You write |
|---|---|
| A text in a component | `const t = useT("panel")`, then `t("config.title")` or `t("home.greeting", { username })` |
| A text whose English source is not a catalog (a `system.json` label, an API code) | `useLookup()`: `lookup(namespace, key) ?? otherText` |
| The text of a failed call | `useErrorText(namespace)` from `@agento/api`: `errorText(e, "config.load_failed")` |
| A number | `useFormat().number(n)`; a number given as a `{param}` is formatted for you |
| A share out of 100 | `useFormat().percent(share)`: `42%`, with the digits and the sign of the language. Do not write a number as JSX text: `numbers.test.ts` fails on it (an id is `String(id)`) |
| A duration in seconds | `useFormat().duration(seconds)`: `42s`, `3m 05s`, `1h 02m`; `—` for `null` |
| A job status (`TODO`, `RUNNING`, …) | `useJobStatusText()`: `statusText(job.status)`. Keep the raw value for a filter or a request |
| A sentence that holds an element | `withElements(t("form.next_run"), { time: <Timestamp value={at} /> })`: the catalog text has a `{time}` hole, and you do not give that param to `t` |
| The product name | `PRODUCT_NAME` (not translated) |
| The browser tab title | `PageHeader` sets `<title> · Agento`; a screen with no `PageHeader` calls `useDocumentTitle` |

- **A key is a literal at the call.** `catalog.test.ts` reads every `t("…")` call and fails on a
  key that `en.json` lacks, and on a `t(…)` whose key is not a literal. A table of texts is a
  function that takes `t` (`adminScreens(t)` in `Shell.tsx`), not a module constant.
- **Lookup order:** the user's language, then the English that the server sent with it, then the
  English built into the bundle (`generated/messages.ts`, written by `npm run gen`), then the key.
  A text that falls back to English uses the English plural rules.
- **An API error never shows the server's words.** `useErrorText` reads `<namespace>:error.<CODE>`,
  then `panel:error.<CODE>`, then the fallback key you give (in `<namespace>`, then in `panel`).
  A refusal can have a `reason`: a second, more exact code beside a wide `code` that older
  clients know (the scheduler's `code: "upload"` with `reason: "UPLOAD_TOO_LARGE"` and
  `params.max_bytes`). The text of a known reason comes first, in the same two namespaces and
  with the same `params`; an unknown reason reads the code. This is true for every module.
  A text that `panel` holds (`error.generic`, the `error.UPLOAD_*` texts) is not copied into a
  module catalog. `tests/unit/test_refusal_contract.py` fails when a code of the Python sources
  has no text in English and in Polish, and when the web layer, the framework or a module raises
  a coded exception (or a subclass of one) with no `code` ([panel.md](../architecture/panel.md#error-codes)).
- **Derived keys.** The Config screen reads `<module>:config.<field path>.label`, `.description`
  and `.option.<value>`, and `<module>:module.title`; the nav reads `<module>:nav.<route path>`,
  and the job Details drawer reads `<module>:joblink`. With no such key the manifest's or the
  descriptor's own text shows. The English source of `nav` and `joblink` is the descriptor
  (`<module>/panel/index.ts`); `catalog.test.ts` fails when a translation lacks one of them.
- **A literal text fails lint.** In `packages/ui/src`, `panel/src` and `<module>/panel`, ESLint
  (`no-restricted-syntax`, the `LITERAL_TEXT` selectors in `eslint.config.js`) refuses a text with
  a letter in JSX and in a text prop or field (`label`, `title`, `placeholder`, `aria-label`,
  `description`, `message`, `alt`, …), also inside a ternary or a template string. Tests, stories
  and the descriptors are exempt. The rule does not see a text in a variable that a prop then
  receives: put such a text in the catalog too.
- **A message is one sentence.** ESLint (the `WHOLE_SENTENCES` selectors) refuses a plural chosen
  in code (`n === 1 ? … : …` with a text or a `t(…)` in a branch), two `t(…)` calls joined with `+`
  or in one template string, and a template string that puts letters beside a `t(…)`. Use a plural
  message with `count`, a `{param}` hole, or `withElements`.
- **No guard is switched off.** `catalog.test.ts` fails on an `eslint-disable` comment in a shipped
  file that names `no-restricted-syntax` or names no rule.
- **A translation is a translation.** `catalog.test.ts` fails when a translated text equals the
  English text (the words that are the same in both languages are in
  `SAME_WORD_IN_BOTH_LANGUAGES`), and when a text does not use the word that the
  [glossary](i18n.md#glossary) gives for a term.
- **No key without a reader.** `catalog.test.ts` fails on an English key that no source asks for.
  It knows a family of keys from the call that builds them (``lookup("ui", `job.status.${status}`)``).
- **Nothing is keyed on the language.** `SessionI18n` (`panel/src/I18n.tsx`) gives the catalog that
  `catalogStore.ts` holds. A language change (`switchLanguage`) re-renders the
  screens and remounts none, so a draft and a stream stay. The profile settings are a dialog
  (`panel/src/ProfileDialog.tsx`) that the user name in the header opens over the current screen:
  there is no profile route, so the change needs no navigation. The store keeps the newest whole
  catalog of each language for the session. `switchLanguage` gets the catalog first and saves
  second: for a language it already holds it asks only if the version changed (`If-None-Match`,
  304 with no body). The save sends the version of that catalog in `If-Match` (`apiFetch`
  option `ifMatch`); the body is `{ "ui_locale" }` only. When the server's catalog is
  another version (a `web` restart between the two calls) it answers 409 `CATALOG_CHANGED` and
  stores nothing: the store drops its copy, reads again and saves again, once; a second refusal
  is a failed change (`panel:error.CATALOG_CHANGED`). After a save the store shows its catalog
  only when the answer names the same language and version; if not, it shows the language of the
  answer. A save that worked closes the dialog and shows one message in the new language; a
  sign-out removes the messages of the session. A failed read or a refused save changes nothing, and the dialog shows the
  error with Save enabled. Only one change runs at a time, and its state (`getLanguageSwitch()`:
  the `pending` language, or the change that `failed`) is in the store, not in the form: a dialog
  that closes and opens again during a save shows the pending language with Save disabled, and a
  failure that came while it was closed shows when it opens (the next close forgets it). The end
  of the session drops every copy and the shown catalog; a read or a change
  that an ended session started changes nothing. This holds by construction. The store has one
  object per session (`current`, with the copies), and the end of the session puts a new one in
  its place. An operation takes `current` once, in its first statement, and gives it to every
  step: a copy goes into that object, which nothing reads after the end. Everything else (the
  shown catalog, the language change, each request) goes through `live(session, effect)`, which
  checks the session and runs the effect in the same call, with no `await` between. An `await`
  is free; an effect after it is not. Do not read or write `held`, `languageSwitch` or `current`
  and do not call `hold`, `note` or a request in an `async` function outside `live(…)`:
  `catalogStore.interleaving.test.ts` scans the source for that and ends the session at every
  microtask between an answer and the next step of every path. Two shows in one session go by
  start order: a read shows its catalog only when no show that started later is on the screen,
  so a read that answers after a language change does not put its language back.
  `Shell` renders nothing without a user, so a
  sign-out shows no English frame before the sign-in page.
- **A text is made at render.** State, a ref and a store hold the error or the key with its
  params, never the result of `t`, `lookup`, `errorText` or a formatter: a kept text stays in the
  old language after a change. Keep a failure as the error (`useState<unknown>`) or as a
  `Failure` (`{ error?, fallbackKey }` from `@agento/api`) and write
  `errorText(failure.error, failure.fallbackKey)` in the JSX; `catalog.test.ts` reads a literal
  `fallbackKey:` as a key. `storedTexts.test.ts` (TypeScript checker) fails on such a call in the
  argument of a state setter, in a promise or mutation callback, in a `catch` block, and in a
  function given to a call that does not run it in the same render (an array method and a React
  hook are fine). It does not follow a text through a variable, so the rule is yours to keep. One
  exception: the message of `notifications.show(…)`. A notification is the record of one event,
  so it is written once, in the language shown at that event, and it does not change later.
- **A fixed option of an `options_source` is a message.** The options of a source are data and
  show as sent (an IANA zone name). A fixed option carries `label_key`, and Config reads
  `panel:config.option.<label_key>` (`Browser time zone`); `label` is the English source.
- **Do not cut an ISO string for a date** (`slice`, `replace("T", " ")`): the result has one
  form in every language. Use `Timestamp`, or `WallTime` for a server wall time.
- **A React key is a stable id.** Never a `label`, a `title` or another text a user reads: a
  translation changes it (the row remounts on a language change), and two texts can be equal.
  `RowAction`, `RowDetail` and a `ThreadList` group have a required `id`; ESLint refuses a text
  field in a `key` (UI-6).
- **Every routed screen names the tab**, also in its error state and when its module is off:
  `PageHeader` with a text title does it, any other state calls `useDocumentTitle`. Do not put the language in a `key`.
- **No English first.** `requireSession` awaits `ensureCatalog` before the first screen, and
  `Login` waits for `GET /api/panel/bootstrap`. Each read has a 2 s deadline; a failure shows the
  bundled English.
- **The catalog store holds the session's catalog only**, or the bundled English when there is
  no session. The sign-in texts are public and belong to the `Login` route: it keeps them in its
  own state and gives them to its own `I18nProvider`, and names its language with
  `useScreenLanguage` (`I18n.tsx`, the one writer of `html lang`). So a signed-in user who opens `/login` and
  goes back finds the account's language as it was. The store exports no function that takes a
  catalog from a caller (`catalogStore.interleaving.test.ts` holds the export list). A screen
  that needs no session must not write the store or the session state of `@agento/api`.
- **A screen test in a second language** uses `renderInLocale("pl", path, ui)` from
  `panel/src/routes/admin/testing.tsx`: it renders with the shipped catalog of that language.
  Assert a landmark (the heading) and `document.title`.

## Admin screens

The admin TUI's screens are in the panel under the **Administration** group:
Dashboard (`/`), Jobs, Agents, Credentials, Tools, Skills and Config (`/admin/<name>`), plus Users.
A screen shows only when the session's `user.acl` holds its leaf (`useCan("admin.jobs")`); `admin`
holds every leaf ([panel.md](../architecture/panel.md#roles-and-grants)).
Each is a lazy route in `panel/src/routes/admin/`, so the entry chunk stays inside its budget. They
call `/api/admin/*` ([panel.md](../architecture/panel.md#admin-screens)).

- **The scope is in the URL.** `ScopePicker` (Default / Workspace / Agent view) writes
  `?scope=&scope_id=`, so a reload or a link from Agents ("Open config") keeps it. Tools, Skills
  and Config use it.
- **Jobs pages, searches and sorts in the database.** See [Server grids](#server-grids).
- **Jobs columns are picked per browser.** The Columns icon on Jobs opens a checkbox list of the
  job fields (`ORDER` in `routes/admin/Jobs.tsx`, one order for the list and the table). The choice
  is kept in `localStorage` (`agento.admin.jobs.columns`); a dot on the icon marks a non-default set.
  Actions is always shown, and the last visible column cannot be unchecked.
- **A secret is never shown.** A secret config row shows `Set` or `Not set` and the editor is
  disabled with the CLI hint. A credential shows that it has an error, never the message.
- **What the panel cannot do, it names.** Replay and build show the CLI command with a copy
  button ([docs/cli/admin.md](../cli/admin.md)).
- **ENV overrides are not shown.** `CONFIG__*` ENV overrides are not visible on the Config screen;
  `bin/agento config:resolve <path>` shows the effective value. The module list shows a title made
  from the key (`agent_view` → "Agent View"); the URL keeps the key.
- **Access and enablement are the same tree.** Role resources, Tools and Skills all render
  `routes/ResourceTree.tsx`: a Mantine `Tree` of checkboxes with search, expand/collapse, select
  all/clear, and the sticky `UnsavedBar` (Reset and Save as icons, UI-7). Below 48em a row's info
  (badges, labels) goes to its own line under the name, indented past the checkbox, so no pill is
  cut and none reads as the next row's. The page gives it one
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
- **Tool labels are admin only.** `GET /api/admin/tools` sends each tool's `requires` to every
  reader and its `catalog` (labels, review status, `revision`) to the `admin` role only; the label
  routes answer 403 to any other role ([panel.md](../architecture/panel.md#admin-screens)). The
  panel checks the role too (`useSession()?.role === "admin"`): another role sees no label, no tab
  and no warning, even if a response carried them. Tools has three tabs in `?tab=`: **Enablement**
  (every reader), **Catalog** and **Labels** (admin). The Enablement panel stays mounted, so its
  draft survives a tab switch; Catalog and Labels mount only while shown.
- **The catalog polls.** The minute `tool:sync-catalog` adds tools and sends a changed contract back
  to review, so Catalog (`ToolCatalog.tsx`, its own query key over the default scope) and Labels
  refetch every 60 s (UI-8; the Enablement tree reads the label names without a poll), and every classification or label write invalidates the catalog,
  the labels and each scope's tool tree. Its filters (name, module, status, labels) AND together; a
  label filter needs every selected label. Before the first sync the tab names the command. A row
  has two columns, Tool and Actions: the Tool cell holds the name, the review state, the module and
  the labels, so a phone shows the whole row with its Classify action.
- **Review run.** While a tool is to review, a banner counts them and offers **Start review**: it
  sets the status filter, remounts the table (a header sort goes back to catalog order) and opens
  the first tool in the drawer at "Reviewed", with "N left" under its title. **Save and next** saves and opens the next; **Skip**
  opens the next without a save. The run keeps its own record of what it showed and of each tool
  the server answered as reviewed, with that revision (a later sync that moves the revision counts
  the tool again); a save that answers (or fails) after its drawer moved on or closed does nothing to the drawer shown now, so its one end message ("All tools are reviewed." or "Review paused. N tools to
  review.") never reads a catalog refetch that has not come back; a save in the run shows no
  message of its own.
- **Classify in a drawer.** `ToolClassifyDrawer.tsx` keeps its form across a refetch. When the
  row's `revision` moves under it, it shows "Changed on the server — reload"; Save still sends the
  revision it was opened on, so the server answers 409 (a notification, then a refetch). One new
  chip goes in the classification itself (`new_label`); more new chips are created with
  `POST /api/admin/tool-labels` first. The review status is sent only when the admin changed it: in a
  plain Classify a label edit never approves a contract; a review run opens each tool at
  "Reviewed", so its **Save and next** approves the tool unless the admin picks "To review". The form waits for the label list: without it a chip
  cannot be told from a new label. A selected label is held by its key, so a rename elsewhere does
  not change it; typed text becomes a new label only with Enter (not on blur, a comma or a paste), and one label
  is one chip, however it was typed. The label input comes last, under the Review control, and its
  hints (the risk groups, the `--stub-tools` keys) are its description: its open dropdown covers
  only the buttons. It has no autofocus, and the form is locked while a save runs.
- **The lethal trifecta warning.** The editor computes it from the unsaved draft (`trifecta.ts`):
  a tool counts when it and every master up its `requires` chain are checked, over all items (a
  search changes nothing). `TrifectaAlert` shows above the tree. The conversation shows each run's
  frozen `tool_risk` above its turn: a red `TrifectaAlert` for `trifecta: true` ("Lethal trifecta")
  and `"possible"` ("Possible lethal trifecta"); a yellow "Assessment incomplete" note when
  coverage is not complete without a trifecta (a `pending` direct check, direct MCP servers named,
  an error); nothing for an old run (`null`). A top-level run whose warning equals the previous
  assessed run's (verdict, tools per group, gaps) shows it compact: still red, "Same risk as the
  last assessment.", the groups behind an underlined **Show tools** link in the alert's own text
  color. A run with no assessment in between is skipped. The first run, a changed risk, a sub-agent card and
  the single-job view show it in full.
- **One risk vocabulary.** `@agento/ui` `ToolRisk.tsx` holds the three risk label keys and names,
  each label's icon (`labelIcon`, `LabelBadge`), the review state (`ReviewState`: an icon and a
  word, no hue, so a fresh install's 62 unreviewed rows are not a wall of one color) and
  `TrifectaAlert`. The admin screens and the conversation module panel (which may import only
  `@agento/ui`) use it, so a risk looks the same everywhere. A label is a neutral badge; its hue is
  on the icon only (3:1 against the badge face in both themes, `components.test.tsx`), and its
  shape and name carry the meaning without color. A long name (up to 100 characters) wraps in its
  badge; label names and keys break anywhere (`routes/layout.ts` `wrapAnywhere`, with the shared
  `TIGHT` gap). A label key is looked up in a `Map`, so a custom label such as "constructor" gets
  the tag icon. Panel only: no `.ag-*` twin.
- **Mouse and keyboard.** `DataTable` activates a row on Enter; each admin table also has a button
  column (Details, Usage, Edit) for the mouse.

## Server grids

A grid over a table that grows without bound pages, searches and sorts in SQL, never in the
browser. Jobs is the first one; another grid uses the same three parts:

- **`web/grid.py`** checks the query once: `page` (1–100 000), `page_size` (25, 50 or 100), `q`
  (trimmed, at most 200 characters), `sort` (one of the keys the route lists) and `dir`. A bad
  value is 400. `contains(q)` makes a literal `LIKE` pattern (`%` and `_` match themselves), and
  `page_body()` makes `{rows, total, page, page_size}`. It builds no SQL: the route maps each sort
  key to SQL itself and always adds the primary key as the tie-break.
- **`useGridParams`** (`@agento/ui`) keeps `page`, `q`, `sort`, `dir` and the page's filters in
  the URL, so a reload, Back and a shared link show the same page. The search box writes `q`
  after `SEARCH_DEBOUNCE_MS` (300 ms); any change but a page change goes back to page 1. Use it
  with `placeholderData: keepPreviousData`, so the table does not blank while the next page loads,
  and move to `lastPage()` (with `replace`) when the answer for the current URL has fewer pages.
  Give it the sort keys the route accepts (`sorts`) and each filter's values (`filters`): a URL
  value the server would refuse (an old or hand-made link) reads as its default, not a 400. The
  search is stripped and counted as `grid.py` does; a search box longer than 200 characters says
  so (`searchError`) and sends nothing until it fits.
- **`Pager`** shows `51–100 of 1,234` and Previous / Next; it is not shown when one page holds all.
  `DataTable` with `sort` + `onSortChange` sorts on the server: a header click reports the new sort
  and the rows stay in the server's order. A header cycles through its first direction, the other
  one, and back to the page's default sort. The sorted header shows a chevron for its direction; the
  rule is in `agento-ui.css`, keyed on `aria-sort`, so `<ag-table>` shows the same mark (kit 1.5.0).
  A page change scrolls the grid's top back into view when it is above the fixed header.

The Jobs SQL (`framework/admin/data.py` `_job_rows`) is a deferred join: the inner query sorts and
counts only ids (`COUNT(*) OVER ()` for a search), the outer one reads the full rows of one page.
A search sorted by id, type, status, view or reference reads the narrow index `idx_job_search`
(type, status, agent view, reference, id), not the wide rows; a search sorted by any other column
needs that column, so it reads the rows. Measured on 500 000 realistic jobs with a 128 MB buffer pool: page 1 29 ms, page 1000
58 ms, a search 0.2–0.5 s, a sort by view 0.15 s, a sort by duration 0.55 s, a broad search sorted
by duration 1 s. A sort that the index does not cover scans the table, so keep job retention in
mind (ROADMAP.md).

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
import graph. So the launch handshake (`openLaunch`, which uses the miniapp bridge) is in
`@agento/api`, and a row action icon is a named action of `@agento/ui` (`OpenAction`,
`EndAction`, `DeleteAction`, `MakeCurrentAction`).

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
| Popup blocked, API refusal closes the window, bridge before POST, code in no URL or cache, a preview never reuses an approved launch | `packages/api/src/launch.test.ts` |
| Apps catalogue, approval page (approve CAS, revoke, preview) | `modules/miniapps/panel/miniapps.test.tsx` |
| Schedules list groups, form create/edit (only the changed fields), preset and Custom recurrence, preview, Run now disabled while dirty, Jira row read-only, share, owner change, delete, a message for every refusal code | `modules/scheduler/panel/scheduler.test.tsx` |
| Artifacts list, versions, make current CAS, delete after the code is typed | `modules/versioned_artifacts/panel/artifacts.test.tsx` |
| Lethal trifecta over a draft: three tools, one missing, all on one tool, `requires` master unchecked, unknown master, cycle | `panel/src/routes/trifecta.test.ts` |
| Tools tabs for admins only; the warning follows the draft without a save and survives a search; labels on leaves; nothing for another role | `panel/src/routes/admin/Tools.test.tsx` |
| Catalog filters AND, one save creates and assigns a chip, extra chips first, 409 refetches, a refetch keeps the drawer's edits and shows the changed notice, Reload, the empty catalog | `panel/src/routes/admin/ToolCatalog.test.tsx` |
| Built-in labels have no actions, delete names the usage, a taken name is refused inline | `panel/src/routes/admin/ToolLabels.test.tsx` |
| The run warning: two runs, different warnings; none for a non-admin or an old run; the incomplete note; pending → recorded on the poll; possible trifecta | `modules/conversation/panel/conversation.test.tsx` |
| Roles list, Delete kept for a built-in or used role, Add role; the role tree saves one PUT, locked leaves are never sent, a search keeps hidden checks, a scope switch asks before it discards, the global scope shows the Admin leaves | `panel/src/routes/Roles.test.tsx`, `panel/src/routes/RoleEdit.test.tsx`, `panel/src/routes/roleTree.test.ts` |
| Probe 404 / 2xx / network error → nav and NotAvailable | `panel/src/registry.test.tsx` |
| The example miniapp decodes the toolbox action envelope | `packages/miniapp-kit/kit.test.ts` |
| Dialog focus trap and focus return | `packages/ui/src/components/Forms.stories.tsx` (`DialogFocus`) |
| Stream opens after the page's newest id, persisted events refetch and never write the cache, 10 s / 30 s reconcile polls, follow-live and "N new events", reset to the newest page on resume, older-page anchor, read-only channel thread | `modules/conversation/panel/conversation.test.tsx` |
| Dedupe by id, tool pairing, runs grouped by execution, the final-answer rule over split pages, partial segments, tool groups, attempts folded into one turn | `modules/conversation/panel/timeline.test.ts` |
| The rail's Shared with me list and its "Load more" keyset page | `modules/conversation/panel/conversation.test.tsx` |
| Queued / Running status line, live partial text, one failure with Retry, typing while busy | `modules/conversation/panel/conversation.test.tsx` |
| Tool summary per tool and without input, durations; thread day groups | `modules/conversation/panel/tools.test.ts`, `modules/conversation/panel/model.test.ts` |
| `Markdown` renders a code fence and a table; raw HTML stays text; opt-in highlight; composer keys, tool row, Retry, thread list, menu button | `packages/ui/src/components/components.test.tsx` |
| WCAG AA contrast, catalogue completeness, skill drift, kit immutability | `packages/miniapp-kit/kit.test.ts` |
| The three date formats in a fixed zone, the provider, `Timestamp` with no provider | `packages/ui/src/components/Timestamp.test.tsx`, `panel/src/DisplayFormat.test.tsx` |
| With no format set a language writes its own date, an operator's format wins, the zone stays | `panel/src/DisplayFormat.test.tsx`, `packages/ui/src/components/Timestamp.test.tsx` |
| `format`, plural rules, the lookup order, an unknown language falls back to English, `useDocumentTitle` | `packages/ui/src/i18n.test.tsx` |
| A language the browser has no data for (`qaa`) shows its own texts and writes numbers, plural forms and dates as English, whatever the default language of the runtime is; only `intl.ts` calls `Intl` | `packages/ui/src/intl.test.ts`, `packages/ui/src/i18n.test.tsx`, `numbers.test.ts` |
| A catalog, session or save answer with status 200 that has another shape (not JSON, a missing field, a wrong type, a message or a language tag that the server's rules refuse, a body that never ends) is a failed read: bundled English on sign-in and in the store, no save sent, no session started | `panel/src/routes/Login.test.tsx`, `panel/src/catalogStore.test.ts`, `packages/api/src/api.test.ts` |
| `@agento/ui` components in Polish | `packages/ui/src/components/polish.test.tsx` |
| An error shows the text of its code, never the server's | `packages/api/src/errorText.test.tsx` |
| One catalog body per (language, version) in a session; a switch to a held language whose new read fails saves nothing; the end of a session drops the catalogs, also with a read pending; the 2 s deadline | `panel/src/catalogStore.test.ts` |
| Sign-in waits for its language, errors by code, a sign-in lands in the user's language | `panel/src/routes/Login.test.tsx` |
| A signed-in user opens `/login` and goes back: the screen and the store keep the account's catalog (real routes) | `panel/src/loginRoundTrip.test.tsx` |
| A sign-in during a pending logout is sent after the logout answer or failure; the new session cookie is the last one written; a logout with no answer is aborted at its deadline; a logout ends the tab's session only on a 2xx or a 401 | `packages/api/src/lateAnswers.test.ts` |
| A failed sign-out shows its text and stays on the page; the next one leaves | `panel/src/Shell.test.tsx` |
| A language save with no answer keeps the change pending; no second save starts | `panel/src/catalogStore.test.ts` |
| The profile dialog: a language change keeps state with no remount and no title change, a failed save changes nothing, focus returns to the header button | `panel/src/ProfileDialog.test.tsx` |
| A language change from the header keeps the route, the draft and the one stream of an open conversation, and translates a failed attachment (real router) | `panel/src/languageSwitch.test.tsx` |
| No number is written as JSX text without a locale formatter | `numbers.test.ts` |
| No text of `t`, `lookup`, `errorText` or a formatter goes into state or is made in a late callback | `storedTexts.test.ts` |
| One language change at a time; a dialog that opens again during a save shows it as pending; a failure that came while it was closed | `panel/src/catalogStore.test.ts`, `panel/src/ProfileDialog.test.tsx` |
| The fixed time-zone option in Polish with the IANA names as they are; the English source equals the Python label | `panel/src/routes/admin/Config.test.tsx`, `tests/unit/framework/harness/test_options_source.py` |
| A next run of a schedule is the server's wall time, written the Polish way, also for a zone the browser does not know; a display zone the browser does not know is written in UTC and named | `modules/scheduler/panel/scheduler.test.tsx`, `packages/ui/src/components/Timestamp.test.tsx` |
| The end of a session at each microtask after each answer of `ensureCatalog` and `switchLanguage` (the 409 retry too) leaves no copy, no shown whole catalog and no further request; an operation reaches the store's state and the requests only through `live(…)`; of two shows the one that started last wins | `panel/src/catalogStore.interleaving.test.ts`, `panel/src/catalogStore.test.ts` (real `apiFetch`) |
| No function of `@agento/api` writes module state after an `await` with no session check between; `boot`, `saveLocale`, `logout` and `openLaunch` with the session ended at each microtask after the answer | `packages/api/src/lateAnswers.test.ts` |
| A refusal with a `reason` shows the text of the reason with its params; the scheduler form says why an attachment was refused | `packages/api/src/errorText.test.tsx`, `modules/scheduler/panel/scheduler.test.tsx` |
| A catalog that changed between the read and the save: one re-read, then a failed change with no session change; an answer in another language | `panel/src/catalogStore.test.ts`, `panel/src/ProfileDialog.test.tsx` |
| Every route sets the tab title, also with every read failed and with its module off | `panel/src/titles.test.tsx` |
| A cold load shows the first screen in the stored language | `panel/src/coldLoad.test.tsx` |
| Polish holds every English key with equal parameters, plural forms match the language, every literal key exists, no English key is unused, each descriptor `nav` and `jobLink` has a translation | `catalog.test.ts` |
| `withElements` puts an element in its hole | `packages/ui/src/i18n.test.tsx` |
| Job status, duration and the job link in two languages | `panel/src/routes/admin/Jobs.test.tsx` |
| A config test `error` result reads its text by code | `panel/src/routes/admin/Config.test.tsx` |
| Every refusal code has a text in English and in Polish | `tests/unit/test_refusal_contract.py` |
| Import boundaries, bundle budget | `boundaries.test.ts` |
| Every story renders with no a11y violation; panel card = miniapp card in light and dark | `packages/ui/src/**/*.stories.tsx` |
| Each Mantine component = its `.ag-*` twin (computed styles and layout), light and dark | `packages/ui/src/acceptance/Parity.stories.tsx` |
| The wheel ships the panel and the kit (runs after the build) | `tests/check_wheel_frontend.py` |
| Proxy routes and cache headers | `tests/unit/framework/docker/test_proxy_config.py`, `docker/smoke/proxy-smoke.sh` |
