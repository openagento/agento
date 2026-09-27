# miniapps

Core module that runs a [versioned artifact](versioned-artifacts.md) version as a **miniapp**: a
page on the apps origin that a user launches from the panel, and that may call a fixed set of
toolbox tools (its **actions**) with that user's rights. No LLM is in the path. Design: PRD E6;
panel side: [../architecture/panel.md](../architecture/panel.md).

| File | Contents |
|---|---|
| `module.json` | `sequence: ["versioned_artifacts", "web"]`; tools `miniapp` (master switch), `miniapp_get_launch_spec`, `miniapp_list` |
| `sql/001_miniapp_activation.sql` | the `miniapp_activation` table |
| `toolbox/miniapps.js` | manifest parser, activation, the two tools |
| `toolbox/launch-source.js` | `authSources = [["launch", checkLaunch]]` |
| `toolbox/cli.js` | the operator side of activation (`miniapp:*` commands) |
| `sdk/bridge.js`, `sdk/agento-sdk.js` | the `postMessage` bridge, panel side and app side |

## Three states

A version is in one of three states, and they stay apart:

| State | Means | Made by |
|---|---|---|
| **saved** | a `versioned_artifacts` version | `versioned_artifact_save_version` |
| **reachable** | its files are served, but only under a live launch or a share | `save_version` materializes it (the HTTP exposure boundary) |
| **activated** | a `miniapp_activation` row whose fingerprint equals the version's manifest | `miniapp:activate` (operator only) |

A launch of a version that is not activated is **files-only**: the page loads, and it can call no
action. An activation whose fingerprint no longer matches the version's `miniapp.json` counts as
not activated.

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

The fingerprint is the SHA-256 of the file's bytes, at most 64 KiB. The file is read through
`versioned_artifacts`' service (`readVersionFile`), the only code that reaches its storage engine.

## Activation

```bash
uv run bin/agento miniapp:activate <artifact_code> <version_id> [--actions a,b]
uv run bin/agento miniapp:deactivate <artifact_code> <version_id>
uv run bin/agento miniapp:list
```

See [../cli/miniapp.md](../cli/miniapp.md). `--actions` must be a subset of the manifest's
actions (default: all of them). There is **no tool equivalent**: `agent_view_id` is asserted by
the caller, so an agent must not decide what a user's browser may call. Every activate and
deactivate writes a `versioned_artifact_audit` row (`miniapp.activate`, `miniapp.deactivate`).
There is no event: the change is made in the toolbox (Node), which has no event mechanism.

## A launch

1. The panel calls `POST /api/launches` with `{agent_view_id, artifact_code}`. `web` resolves
   `current` (`versioned_artifact_get_current`), then asks `miniapp_get_launch_spec` for that
   version, both with the user's own `user_session` capability, under the artifact's retention
   lock.
2. An activated version pins its `manifest_fingerprint` and `allowed_actions` into the `launch`
   row. A version that is not activated, or a spec tool the user may not call (not granted or not
   enabled: `403`/`404`), is a files-only launch. A toolbox failure, a tool error or a malformed
   answer is `503`, never a files-only guess.
3. The page calls an action through the bridge. The panel posts it to
   `POST /api/launches/<launch_id>/actions/<tool>` with `{"arguments": {…}}`.
4. `web` checks that the launch is the caller's, redeemed and live (else `404`), and that the tool
   is in its `allowed_actions` (else `403`). It then mints one single-use `miniapp` capability:
   `tool_ceiling` = the launch's actions, the app triple (`artifact_code`, `version_id`,
   `launch_id`), a TTL that never outlives the launch. The raw token lives in that call only.
5. The toolbox runs the `launch` checker on the call: the launch is still redeemed, live and not
   revoked, the user is active and still holds `artifact.launch` in the scope, and the version is
   still activated with the fingerprint the launch pinned. The tools the call may reach are the
   capability's ceiling ∩ the role's tool grants for the scope ∩ `is_enabled`. The dispatcher
   writes a `tool_invocation` row with the app triple.

A miniapp never gets an `internal_rest` capability, and no raw bearer reaches the page.

## Tools

Both are gated on their own `tools/<name>/is_enabled` key and `require` the master switch
`miniapp`, off by default. A user also needs a **tool grant** for `miniapp_get_launch_spec`
(and `miniapp_list` for the catalogue) in the scope: without it every launch is files-only.

| Tool | Answers |
|---|---|
| `miniapp_get_launch_spec` | `{activated: false}`, or `{activated: true, manifest_fingerprint, allowed_actions}` for one version |
| `miniapp_list` | `{miniapps: [{artifact_code, version_id, title}]}`: each usable artifact whose `current` is activated |

`GET /api/agent-views/<id>/miniapps` returns the second list to the panel.

## The SDK bridge

All apps share one origin, so `event.origin` cannot tell two apps apart (PRD E6 §8).

* **Panel** — `createLaunchBridge({appWindow, appsOrigin, launchId, onAction})`. It accepts a
  message only from the exact window it opened (`event.source === appWindow`), from the apps
  origin, and — past the handshake — carrying this `launch_id`. It replies to `appsOrigin`,
  never `'*'`.
* **App** — `createAgentoSdk({panelOrigin})` → `{ready, callAction(tool, args), close()}`.
  At most `MAX_IN_FLIGHT` (16) calls wait for an answer; one more resolves `{status: 429}` at
  once and posts nothing.
  `panelOrigin` is a trusted value the app gives itself (from its agent_view instructions), never
  one read from a message or the URL. It must be one exact `https:` origin (or `http://localhost`).
  The SDK refuses to start without `window.opener`, accepts messages only from the opener at
  `panelOrigin`, and posts only to `panelOrigin`.

Messages: app → `{type: "agento.ready"}`; panel → `{type: "agento.hello", launch_id}`;
app → `{type: "agento.action", launch_id, id, tool, arguments}`;
panel → `{type: "agento.result", launch_id, id, status, body}`.

## Enable checklist

```bash
uv run bin/agento module:enable miniapps && uv run bin/agento setup:upgrade
uv run bin/agento tool:enable miniapp --agent-view <view>
uv run bin/agento tool:enable miniapp_get_launch_spec --agent-view <view>
uv run bin/agento grant:add --role user --tool miniapp_get_launch_spec --agent-view <view>
uv run bin/agento grant:add --role user --operation artifact.launch --agent-view <view>
# plus a grant and a tool gate for each action the manifest names
uv run bin/agento miniapp:activate <artifact_code> <version_id>
```

**To stop a miniapp's actions**, deactivate the version (`miniapp:deactivate`): the `launch`
checker requires the activation on every call, so live launches lose their actions at the next
call. Turning off `miniapp_get_launch_spec` for a scope makes every later launch there
files-only.

**`module:disable miniapps`** stops every miniapp action: `web` is the only minter of a
`miniapp` capability, and it reads `app/etc/modules.json` per request. With the module off, a new
launch is files-only, `POST /api/launches/<id>/actions/<tool>` is `404`, and the catalogue is
empty. The toolbox reads the same file: the next MCP session has no `miniapp_*` tool, and from
the next toolbox start the `launch` checker is not loaded, so no `miniapp` capability verifies.
