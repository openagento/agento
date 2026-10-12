# miniapps

Core module that runs a [versioned artifact](versioned-artifacts.md) version as a **miniapp**: a
page on the apps origin that a user launches from the panel, and that may call a fixed set of
toolbox tools (its **actions**) with that user's rights. No LLM is in the path. Design: PRD E6 and
DECISIONS.md D-MINIAPP-2; panel side: [../architecture/panel.md](../architecture/panel.md).

| File | Contents |
|---|---|
| `module.json` | `sequence: ["versioned_artifacts", "web"]`; no tool |
| `sql/002_miniapp_activation_per_view.sql` | the `miniapp_activation` table: one approval per (artifact, agent_view) |
| `src/activation.py`, `src/routes.py`, `di.json` | approve, revoke, launch and the catalogue, under `/api/miniapps/` |
| `src/patches/RemoveMiniappTools.py` | removes the switches and grants of the old `miniapp*` tools |
| `toolbox/manifest.js` | the one strict `miniapp.json` parser |
| `toolbox/admin-api.js` | `POST /admin/miniapps/<code>/resolve` for `web` (version → served tree, content id, manifest) |
| `toolbox/launch-source.js` | `authSources = [["launch", checkLaunch]]` and the retention pin |
| `sdk/bridge.js`, `sdk/agento-sdk.js` | the `postMessage` bridge, panel side and app side |

## Three states

A version is in one of three states, and they stay apart:

| State | Means | Made by |
|---|---|---|
| **saved** | a `versioned_artifacts` version | `versioned_artifact_save_version` |
| **reachable** | its files are served, but only under a live launch or a share | `save_version` materializes it (the HTTP exposure boundary) |
| **approved** | a `miniapp_activation` row for (artifact, agent_view) that pins this version and its content | an admin, in the panel (`POST …/approval`) |

The approval stays when the agent publishes a new version: the view keeps the approved version,
and the catalogue shows `update_waiting` until an admin approves the new `current`.

## The manifest

`miniapp.json` at the root of the version's tree. It is strict: unknown keys, a wrong type or a
duplicate action make it invalid.

```json
{ "schema": 1, "title": "Notes", "actions": ["jira_get_issue", "jira_add_comment"] }
```

| Key | Rule |
|---|---|
| `schema` | exactly `1` |
| `title` | 1–200 characters |
| `actions` | at most 64 unique tool names, each `^[a-z0-9_]{1,64}$` |

The file is at most 64 KiB. The toolbox reads it through `versioned_artifacts`' service
(`readVersionFile`), the only code that reaches its storage engine.

## Approval

| Route | Leaf | Does |
|---|---|---|
| `GET /api/miniapps/apps/<code>/approvals` | `miniapps.approve` | the artifact's approvals in the views where the user has `artifact.launch` (`agent_view_id`, `approved_version_id`, `allowed_actions`, `activated_by`, `activated_at`) |
| `GET /api/miniapps/apps/<code>/views/<view_id>/approval` | `miniapps.approve` | the approval (or `null`) and `current` with its manifest actions |
| `POST /api/miniapps/apps/<code>/views/<view_id>/approval` `{expected_version_id, actions?}` | `miniapps.approve` | approve `current` for the view |
| `DELETE /api/miniapps/apps/<code>/views/<view_id>/approval` | `miniapps.approve` | revoke the approval |

All four also need `artifact.launch` in the view (else `404`). Each route names its leaf in
`di.json` `acl`, and `web` checks it before the handler (`403`). The leaves are declared in the
module's `acl.json` (screen "Miniapps": `miniapps`, `miniapps.approve`).

Approve, under the artifact's retention lock (the lock prune and launch take):

1. `web` asks the toolbox `POST /admin/miniapps/<code>/resolve {version: "current"}`. The toolbox
   serves the version's tree again if retention removed it, and answers the version id, its
   `content_id` (the identity of the whole tree) and its manifest.
2. Only `current` can be approved: a version other than `expected_version_id` is `409` with the
   real `current_version`. A missing or invalid manifest is `409`.
3. `actions` must be a subset of the manifest's actions (default: all of them), else `400`.
4. One row per (artifact, view) is written, with `approved_version_id`, `approved_content_id`,
   `allowed_actions` and `activated_by`, and one `versioned_artifact_audit` row
   (`miniapp.approve`). Revoke deletes the row and writes `miniapp.revoke`.

The actor is `service:web:user:<id>`: the same string goes to the toolbox as the capability
subject and into `activated_by` and the audit row. There is no tool for approval: `agent_view_id`
is asserted by the caller, so an agent must not decide what a user's browser may call. There is
no event: `web` is the one writer, and the audit row records it.

**The approval is the grant.** The `launch` checker does not read the role's tool grants. Every
role with `artifact.launch` in the view may call the approved actions. Each action tool must
still be enabled in the view (`tool:enable`); that also gives the tool to the agent of that view.

## A launch

1. The panel calls `POST /api/miniapps/launches` with `{agent_view_id, artifact_code}` (route
   `acl: "self"`). The user needs `artifact.launch` in the view, else `404`. A `preview` key in the
   body is `400`.
2. A preview is its own route, `POST /api/miniapps/previews` with the same body. It needs the leaf
   `miniapps.approve` (else `403`, checked first) and `artifact.launch` in the view (else `404`). It
   launches `current` with **no actions**, with or without an approval.
3. Else the view needs an approval (else `404`). Under the retention lock, `web` resolves the
   approved version (which serves its tree again if retention removed it). A missing version, or a
   `content_id` other than the approved one, is `409 approved version gone`, and the approval row is
   deleted. A version id can repeat after an artifact is deleted and made again; the content id
   cannot, so a new artifact never inherits an old approval.
4. The `launch` row pins the approved version and its `allowed_actions`.
5. The page calls an action through the bridge. The panel posts it to
   `POST /api/launches/<launch_id>/actions/<tool>` with `{"arguments": {…}}`.
6. `web` checks that the launch is the caller's, redeemed and live (else `404`), and that the tool
   is in its `allowed_actions` (else `403`). It then mints one single-use `miniapp` capability:
   `tool_ceiling` = the launch's actions, the app triple (`artifact_code`, `version_id`,
   `launch_id`), a TTL that never outlives the launch. The raw token lives in that call only.
7. The toolbox runs the `launch` checker on the call: the launch is still redeemed, live and not
   revoked, the user is active and still holds `artifact.launch` in the view, and the approval
   for (artifact, view) still pins the launch's version. The tools the call may reach are the
   capability's ceiling ∩ the approval's actions ∩ `is_enabled`. The dispatcher writes a
   `tool_invocation` row with the app triple.

A miniapp never gets an `internal_rest` capability, and no raw bearer reaches the page.

**Retention pin.** `launch-source.js` exports `retentionPins`: the toolbox prune keeps the served
tree of every approved version while `miniapps` is enabled.

## Catalogue

`GET /api/miniapps/agent-views/<id>/catalogue` (leaf `miniapps`) lists the view's approvals:
`[{artifact_code, version_id, title, current_version, update_waiting}]`. `version_id` is the
approved version, `title` and `current_version` come from the store (`GET /admin/versioned-artifacts`),
and no manifest is read per row. An approved artifact that is not in the store any more is left
out and logged. A reachable view without `artifact.launch` answers `[]`, not `404`.

`GET /api/miniapps/agent-views` (leaf `miniapps` or `miniapps.approve`, else `403`) lists the views where the user
has `artifact.launch`.

## Panel

The module ships `panel/` (probe `GET /api/miniapps/agent-views`):

* `/apps` — the catalogue of a view: **Open** launches the approved version, and the open
  launches can be ended. A user with `miniapps.approve` also sees "New version waiting" and a
  link to the approval page.
* `/apps/<code>` — the approval page for one view (`?view=<id>` picks it; else the first view): the approval, `current`, and the actions of
  `current` as check boxes (with "Tool in view" on or off when the user holds `admin.tools`).
  **Approve current**, **Revoke**, and **Preview current** (`current` with no actions).

An agent hands over `<panel_origin>/apps/<code>`.

## The SDK bridge

All apps share one origin, so `event.origin` cannot tell two apps apart (PRD E6 §8).

* **Panel** — `createLaunchBridge({appWindow, appsOrigin, launchId, onAction})`. It accepts a
  message only from the exact window it opened (`event.source === appWindow`), from the apps
  origin, and — past the handshake — carrying this `launch_id`. It replies to `appsOrigin`,
  never `'*'`. At most `MAX_IN_FLIGHT` (16) actions run at once; one more gets `{status: 429}`
  and calls nothing, because the app is untrusted.
* **App** — `createAgentoSdk({panelOrigin})` → `{ready, callAction(tool, args), close()}`.
  At most `MAX_IN_FLIGHT` (16) calls wait, for the handshake or for an answer; one more resolves `{status: 429}` at
  once and posts nothing.
  `panelOrigin` is a trusted value the app gives itself (the agent writes in the `panel_origin` that
  `versioned_artifact_create_draft` returns; never `location.origin`, which is the apps origin), never
  one read from a message or the URL. It must be one exact `https:` origin (or `http://localhost`).
  The SDK refuses to start without `window.opener`, accepts messages only from the opener at
  `panelOrigin`, and posts only to `panelOrigin`.

Messages: app → `{type: "agento.ready"}`; panel → `{type: "agento.hello", launch_id}`;
app → `{type: "agento.action", launch_id, id, tool, arguments}`;
panel → `{type: "agento.result", launch_id, id, status, body}`.

## Writing a miniapp

A miniapp page is hand-written HTML over the shared kit, served by `proxy` at `/_ui/<version>/`
on the apps origin: `agento-ui.css` (the same tokens as the panel), `agento-ui.js` (`<ag-table>`,
`<ag-dialog>`, `<ag-copy>`, `<ag-json>`) and `agento-bridge.js` (this SDK, byte for byte). The
`miniapp-build` skill tells an agent the steps and the limits (no server, no credential, no
external call, AI text written at build time, the hand-over an operator needs), and the generated
`miniapp-ui` skill tells it the kit classes; enable both per scope
(`skill:enable miniapp-build`, `skill:enable miniapp-ui`). An example is in
`frontend/miniapps/examples/job-status/`. See [../development/frontend.md](../development/frontend.md#the-miniapp-kit).

## Enable checklist

```bash
uv run bin/agento module:enable miniapps && uv run bin/agento setup:upgrade
uv run bin/agento grant:add --role user --operation artifact.launch --agent-view <view>
uv run bin/agento grant:add --role admin --operation artifact.launch --agent-view <view>
# for each action the manifest names:
uv run bin/agento tool:enable <action> --agent-view <view>
```

Then an admin approves the version for the view (see Approval). A role other than admin needs the
leaf `miniapps.approve` to approve, revoke and preview, and `miniapps` to see the catalogue.

**To stop a miniapp's actions**, revoke the approval: the `launch` checker reads the row on every
call, so live launches lose their actions at the next call.

**`module:disable miniapps`** stops every miniapp action: `web` is the only minter of a
`miniapp` capability, and it reads `app/etc/modules.json` per request, so
`POST /api/launches/<id>/actions/<tool>` is `404` at once. The `/api/miniapps/` routes go when
`web` restarts (`module:enable`/`module:disable` restart it). The toolbox reads the same file:
from the next toolbox start the `launch` checker and the retention pin are not loaded, so no
`miniapp` capability verifies.
