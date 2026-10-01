# Frontend: panel, UI kit and miniapp kit (E8)

The panel is a static React app. `proxy` serves its built files on the panel origin; it calls
only the `web` API that E2, E3 and E6 already ship. The miniapp kit is one stylesheet and a few
custom elements that a miniapp page loads from `/_ui/<version>/` on the apps origin. Both get
their look from one token source, so a panel card and a miniapp card look the same.

## Layout

| Path | What it is |
|---|---|
| `frontend/packages/miniapp-kit/` | Tokens (`src/tokens.ts`), the `.ag-*` rules (`src/components.css`), the custom elements (`src/agento-ui.js`), and the build of `agento-ui.css` |
| `frontend/packages/ui/` (`@agento/ui`) | React components over the `.ag-*` classes, the Mantine theme, and the stories |
| `frontend/packages/api/` (`@agento/api`) | `apiFetch`, the session, the query client, the stream hub, the panel module contract |
| `frontend/panel/` | The app: shell, router, login, users and grants, miniapp launches |
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
npm run gen         # rerun after editing the kit (tokens, components.css, agento-ui.js, catalogue)
                    # or adding a module panel/index.ts — `dev` does not watch the generators
npm run storybook   # component workbench on :6006
npm test            # build, lint, then every unit test and every story in headless Chromium
```

`bin/test` runs `npm test` as step 9. In the dev stack the build is live: `proxy` mounts
`src/agento/framework/web/` directly, so a rebuild needs no container restart.

## Rules

- **A component gets its look from `.ag-*` classes only.** ESLint refuses a `style` or `styles`
  prop in `packages/ui` (stories excepted). Mantine is used for the app shell, the modal, tabs and
  notifications; the presentational components are plain elements with kit classes.
- **No credential in the UI.** The CSRF token lives in a module closure in `session.ts`: never in
  storage, a cookie the page can read, a URL or a log. A write with no token is not sent. The
  launch exchange code is used once, in a hidden form POST, and is never in a URL, a cache, React
  state or the window name.
- **Persisted rows come from REST only.** The stream (`hub.ts`) says "refetch"; only the in-flight
  text and tool rows are built from it, in a bounded store (64 KiB, 200 rows, 8 executions).
- **One teardown.** Logout, the first 401 and a new login call `endSession()`: it clears the query
  cache, closes the stream and closes every launch window.

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
versions stay served, so existing miniapps keep their look.

## Tests

| What | Where |
|---|---|
| CSRF in memory only, A → logout → B leaves nothing, a stale response is dropped | `packages/api/src/api.test.ts` |
| Stream reconnect, refused stream, `cursor_expired`, backoff | `packages/api/src/hub.test.ts` |
| Popup blocked, API refusal closes the window, bridge before POST, code in no URL or cache | `panel/src/launch.test.ts` |
| A mistyped grant scope id sends nothing | `panel/src/routes/Grants.test.tsx` |
| Probe 404 / 2xx / network error → nav and NotAvailable | `panel/src/registry.test.tsx` |
| The example miniapp decodes the toolbox action envelope | `packages/miniapp-kit/kit.test.ts` |
| Dialog focus trap and focus return | `packages/ui/src/components/Forms.stories.tsx` (`DialogFocus`) |
| Transient caps, persisted events refetch and never write the cache, 10 s / 30 s reconcile polls | `modules/conversation/panel/conversation.test.tsx` |
| WCAG AA contrast, catalogue completeness, skill drift, kit immutability | `packages/miniapp-kit/kit.test.ts` |
| Import boundaries, bundle budget | `boundaries.test.ts` |
| Every story renders with no a11y violation; panel card = miniapp card in light and dark | `packages/ui/src/**/*.stories.tsx` |
| The wheel ships the panel and the kit (runs after the build) | `tests/check_wheel_frontend.py` |
| Proxy routes and cache headers | `tests/unit/framework/docker/test_proxy_config.py`, `docker/smoke/proxy-smoke.sh` |
