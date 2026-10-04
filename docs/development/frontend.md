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
| `frontend/panel/` | The app: shell, router, login, users and grants, miniapp launches, the admin screens (`src/routes/admin/`) |
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
- **A `DataTable` cell is a plain function, called on each render.** Do not render it as a component:
  a new component type per render remounts the cell, and an open select in it closes.
- **No credential in the UI.** The CSRF token lives in a module closure in `session.ts`: never in
  storage, a cookie the page can read, a URL or a log. A write with no token is not sent. The
  launch exchange code is used once, in a hidden form POST, and is never in a URL, a cache, React
  state or the window name.
- **Persisted rows come from REST only.** The stream (`hub.ts`) says "refetch"; only the in-flight
  text and tool rows are built from it, in a bounded store (64 KiB, 200 rows, 8 executions).
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
- **Pick by name, never by id.** The Users → Grants tab gives access in a "Give access" sheet: Who
  (role), Where (a workspace or agent view, by name, required), What (tools of that place, or
  operations), then one `POST` per name. A tool that is off at that place is marked "(off here)",
  because a grant does not enable a tool.
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
`components.css`; enable it per scope like any skill.

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
| Stream reconnect, refused stream, `cursor_expired`, backoff | `packages/api/src/hub.test.ts` |
| Popup blocked, API refusal closes the window, bridge before POST, code in no URL or cache | `panel/src/launch.test.ts` |
| The grant sheet cannot send without a place, one POST per name, a refusal names the item | `panel/src/routes/Grants.test.tsx` |
| Probe 404 / 2xx / network error → nav and NotAvailable | `panel/src/registry.test.tsx` |
| The example miniapp decodes the toolbox action envelope | `packages/miniapp-kit/kit.test.ts` |
| Dialog focus trap and focus return | `packages/ui/src/components/Forms.stories.tsx` (`DialogFocus`) |
| Transient caps, persisted events refetch and never write the cache, 10 s / 30 s reconcile polls | `modules/conversation/panel/conversation.test.tsx` |
| WCAG AA contrast, catalogue completeness, skill drift, kit immutability | `packages/miniapp-kit/kit.test.ts` |
| Import boundaries, bundle budget | `boundaries.test.ts` |
| Every story renders with no a11y violation; panel card = miniapp card in light and dark | `packages/ui/src/**/*.stories.tsx` |
| Each Mantine component = its `.ag-*` twin (computed styles and layout), light and dark | `packages/ui/src/acceptance/Parity.stories.tsx` |
| The wheel ships the panel and the kit (runs after the build) | `tests/check_wheel_frontend.py` |
| Proxy routes and cache headers | `tests/unit/framework/docker/test_proxy_config.py`, `docker/smoke/proxy-smoke.sh` |
