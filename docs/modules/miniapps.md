# miniapps

Core module that runs a [versioned artifact](versioned-artifacts.md) version as a **miniapp**: a
page on the apps origin that a user launches from the panel, and that may call a fixed set of
toolbox tools (its **actions**) with that user's rights. No LLM is in the path. Design: PRD E6 and
DECISIONS.md D-MINIAPP-2 and D-MINIAPP-3 (per-app rights); panel side: [../architecture/panel.md](../architecture/panel.md).

| File | Contents |
|---|---|
| `module.json` | `sequence: ["versioned_artifacts", "web"]`; no tool |
| `sql/002_miniapp_activation_per_view.sql` | the `miniapp_activation` table: one approval per (artifact, agent_view) |
| `sql/003_miniapp_settings.sql` | `miniapp` (title, icon), `miniapp_member` (a user or a role, `can_edit`, `can_remove`), `miniapp_activation.manifest_actions` |
| `src/rights.py` | the per-app rights: `view`, `edit`, `remove`, `execute` |
| `src/activation.py`, `src/settings.py`, `src/routes.py`, `di.json` | approve, revoke, launch, the catalogue, settings and members, under `/api/miniapps/` |
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
| **approved** | a `miniapp_activation` row for (artifact, agent_view) that pins this version and its content | a user with `edit` on the app, in the panel (`POST …/approval`) |

The approval stays when the agent publishes a new version: the view keeps the approved version,
and the catalogue shows `update_waiting` (to a user with `edit`) until someone approves the new `current`.

## Rights

Each app has its own rights (D-MINIAPP-3). For user `u` with role `r`:

| Right | Who has it | Gives |
|---|---|---|
| `view` | admin, the creator, a member (a `miniapp_member` row for `u` or for `r`) | the app shows in the catalogue (a disabled tile without `execute`) |
| `edit` | admin, the creator, a member with `can_edit` | title, icon, members; approve, revoke and preview a version |
| `remove` | admin, the creator, a member with `can_remove` | delete the app and all its versions |
| `execute` in view `V` | `view`, an approval in `V`, and admin or (`artifact.launch` in `V` and a tool grant in `V` for **every** action of the approved manifest) | open the app in `V` |

* **Creator** = the panel user whose conversation job ran `versioned_artifact_init`
  (`versioned_artifact.created_job_id` → `job.requester_key = 'user:<id>'` with
  `requester_trust = 'account'`). A job from a channel (email, Jira) has no creator.
* **Admin** has every right on every app. The bypass keeps the other gates: a tool runs only where
  it is enabled for the view (`is_enabled`), and only within the approval's `allowed_actions`.
* `execute` is read again on each launch and on each action call (the toolbox `launch` checker).
  With manifest `[A, B]`, approved `[A]` and a grant for `A` only, there is no `execute`. An approval
  made before `manifest_actions` existed gives no `execute` to a non-admin: approve it again.
* Rights rows hang off the `versioned_artifact` row (`ON DELETE CASCADE`): a delete removes them,
  and `init` never reuses a code whose row is still there.

An app **is** a versioned artifact, so the app settings page (`/apps/<code>`) and the catalogue row both link
to `/artifacts/<code>`, where its versions and their history are. The link shows only while the artifacts
panel answers its probe for this user (the module is enabled and the user holds the `versioned_artifacts`
leaf).

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

## Routes

Every route is `acl: "self"`: `web` lets every signed-in user in, and the handler checks the
per-app right. No `view` of the app is `404` (no disclosure); `view` without the right is `403`.

| Route | Needs | Does |
|---|---|---|
| `GET /api/miniapps/apps` | — | the catalogue (see Catalogue); also the panel probe |
| `GET /api/miniapps/agent-views` | — | the views where the user may approve and preview (admin: every view; else the views with `artifact.launch`) |
| `GET /api/miniapps/apps/<code>` | `view` | `default_title`, `title`, `icon`, `can`; `members` only with `edit` |
| `PATCH /api/miniapps/apps/<code>` `{title?, icon?}` | `edit` | `null` or `""` clears; `icon` is a name from `panel/icons.json` (else `400`) |
| `DELETE /api/miniapps/apps/<code>` | `remove` | the one artifact delete (`versioned_artifacts` `delete_artifact`) |
| `GET /api/miniapps/apps/<code>/member-options` | `edit` | `{users, roles}`: the active users' names and the roles, for the add-member list |
| `PUT /api/miniapps/apps/<code>/members` `{username \| role, can_edit, can_remove}` | `edit` | add or change a member; changing `can_remove` needs `remove` (`403`) |
| `DELETE /api/miniapps/apps/<code>/members/<id>` | `edit` | remove a member; ends the app's live launches in the same transaction, under the retention lock that a launch also takes (the launch checks the rights again inside it) |
| `GET /api/miniapps/apps/<code>/approvals` | `edit` | the app's approvals in the views of `GET …/agent-views` |
| `GET /api/miniapps/apps/<code>/views/<view_id>/approval` | `edit` + view | the approval (or `null`) and `current` with its manifest actions |
| `POST /api/miniapps/apps/<code>/views/<view_id>/approval` `{expected_version_id, actions?}` | `edit` + view | approve `current` for the view |
| `DELETE /api/miniapps/apps/<code>/views/<view_id>/approval` | `edit` + view | revoke the approval |
| `POST /api/miniapps/launches` `{agent_view_id, artifact_code}` | `execute` | launch the approved version |
| `POST /api/miniapps/previews` `{agent_view_id, artifact_code}` | `edit` + view | launch `current` with no actions |

"+ view" means admin, or `artifact.launch` in that view (else `404`). Each write of a setting or a
member writes a `versioned_artifact_audit` row (`miniapp.settings`, `miniapp.member_put`,
`miniapp.member_delete`).

## Approval

Approve, under the artifact's retention lock (the lock prune and launch take):

1. `web` asks the toolbox `POST /admin/miniapps/<code>/resolve {version: "current"}`. The toolbox
   serves the version's tree again if retention removed it, and answers the version id, its
   `content_id` (the identity of the whole tree) and its manifest.
2. Only `current` can be approved: a version other than `expected_version_id` is `409` with the
   real `current_version`. A missing or invalid manifest is `409`.
3. `actions` must be a subset of the manifest's actions (default: all of them), else `400`.
4. One row per (artifact, view) is written, with `approved_version_id`, `approved_content_id`,
   `allowed_actions`, `manifest_actions` (the whole manifest: what `execute` needs grants for) and
   `activated_by`, and one `versioned_artifact_audit` row
   (`miniapp.approve`). Revoke deletes the row and writes `miniapp.revoke`.

The actor is `service:web:user:<id>`: the same string goes to the toolbox as the capability
subject and into `activated_by` and the audit row. There is no tool for approval: `agent_view_id`
is asserted by the caller, so an agent must not decide what a user's browser may call. There is
no event: `web` is the one writer, and the audit row records it.

**The approval is not the grant.** A user runs the app only with `execute` (see Rights): the role
needs a tool grant in the view for every action of the manifest. Each action tool must also be
enabled in the view (`tool:enable`); that also gives the tool to the agent of that view.

## A launch

1. The panel calls `POST /api/miniapps/launches` with `{agent_view_id, artifact_code}`. A view the
   user cannot reach, or no `view` of the app, is `404`; `view` without `execute` (no approval
   included) is `403`. A `preview` key in the body is `400`.
2. A preview is its own route, `POST /api/miniapps/previews` with the same body. It needs `edit` on
   the app and admin or `artifact.launch` in the view. It launches `current` with **no actions**,
   with or without an approval.
3. Else Under the retention lock, `web` resolves the
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
   revoked, the user is active and still has `view` (admin, member or creator), the approval for
   (artifact, view) still pins the launch's version, and — for a non-admin — the role still holds
   `artifact.launch` and a tool grant for every action of the stored manifest. The tools the call may reach are the
   capability's ceiling ∩ the approval's actions ∩ `is_enabled`. The dispatcher writes a
   `tool_invocation` row with the app triple.

A miniapp never gets an `internal_rest` capability, and no raw bearer reaches the page.

**Retention pin.** `launch-source.js` exports `retentionPins`: the toolbox prune keeps the served
tree of every approved version while `miniapps` is enabled.

## Catalogue

`GET /api/miniapps/apps` lists every app the user may view (admin: every artifact of the store),
over all views:

```
[{artifact_code, title, icon, can: {view, edit, remove, execute},
  views: [{agent_view_id, label, version_id, execute, update_waiting?, approve_again?}]}]
```

`title` is the app's title, else the store's title, else the code. `views` lists the views where
the app is approved **and** the user can reach (no label of another view leaks). `update_waiting`
(a newer `current`) and `approve_again` (no stored manifest) show only with `edit`. `can.execute`
is true when one view executes. A user with no rights row gets `[]` with no toolbox call. An app
with rights rows that is not in the store any more is left out and logged.

## Panel

The module ships `panel/` (probe `GET /api/miniapps/apps`):

* `/apps` — the apps as **tiles** (default) or a **list** (the choice stays in the browser,
  `localStorage` key `agento.apps.view`). A tile with `execute` opens the app (a menu picks the view
  when it executes in more than one); a tile with only `view` is disabled ("You may view this app but
  not run it"). In the list, the open icon opens a menu of the views when there is more than one.
  With `edit`, a settings icon; with `remove`, a delete icon that asks first (the only delete in
  the panel). Below: the open launches, which can be ended.
* `/apps/<code>` — the app settings (`edit`). The header is the form: a click on the icon opens the
  icon set, and the pencil (on hover) makes the title a field; a pick, Enter or leaving the field
  saves (an empty title goes back to the agent's). Then the members (add a user or a role from one
  searchable list; the flags), and the approval per view (`?view=<id>` picks it; the actions of
  `current` as check boxes, with "Tool in view" when the user holds `admin.tools`; **Approve
  current**, **Revoke** (asks first) and **Preview current**). An app runs its actions with the
  tools and accounts of one agent view, so each view needs its own approval.

An agent hands over `<panel_origin>/apps/<code>`.

## The SDK bridge

All apps share one origin, so `event.origin` cannot tell two apps apart (PRD E6 §8).

* **Panel** — `createLaunchBridge({appWindow, appsOrigin, launchId, onAction})`. It accepts a
  message only from the exact window it opened (`event.source === appWindow`), from the apps
  origin, and — past the handshake — carrying this `launch_id`. It replies to `appsOrigin`,
  never `'*'`. At most `MAX_IN_FLIGHT` (16) actions run at once; one more gets `{status: 429}`
  and calls nothing, because the app is untrusted.
  A failed action always answers `{ok: false, error: {code, message}}`: when the toolbox or
  `web` gives no reason a user can act on (a refused capability, an ended launch), the panel puts
  one in `message`, so an app shows `message` on the failed row.
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
# for each action the manifest names:
uv run bin/agento tool:enable <action> --agent-view <view>
# a role other than admin runs the app only with a grant for every action of its manifest:
uv run bin/agento grant:add --role user --tool <action> --agent-view <view>
```

Then a user with `edit` on the app (the creator, or admin) approves the version for the view (see
Approval) and adds members on the app settings page. A member sees the app; to run it, the
member's role needs the grants above.

**To stop a miniapp's actions**, revoke the approval: the `launch` checker reads the row on every
call, so live launches lose their actions at the next call.

**`module:disable miniapps`** stops every miniapp action: `web` is the only minter of a
`miniapp` capability, and it reads `app/etc/modules.json` per request, so
`POST /api/launches/<id>/actions/<tool>` is `404` at once. The `/api/miniapps/` routes go when
`web` restarts (`module:enable`/`module:disable` restart it). The toolbox reads the same file:
from the next toolbox start the `launch` checker and the retention pin are not loaded, so no
`miniapp` capability verifies.
