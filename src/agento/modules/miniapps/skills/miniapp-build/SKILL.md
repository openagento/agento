---
name: miniapp-build
description: Use when a user asks for a miniapp, an app, a tool page, a dashboard or an approval screen in Agento — build it as a versioned artifact with miniapp.json, the kit and toolbox actions; never a server.
---

# Build a miniapp

A **miniapp** is a set of static files in one version of a versioned artifact. A user opens it
from the Agento panel (**Miniapps → Open**). The page runs in the user's browser. It can call a
fixed list of toolbox tools, its **actions**, through the panel. Each action runs with the rights
of the signed-in user. Nothing else runs: there is no server of yours, no build and no LLM.

This skill gives the steps and the limits. For the HTML classes and elements, use the
`miniapp-ui` skill. For versioned-artifact error codes, read your versioned-artifacts
instructions.

## The three states

| State | Means | Who does it |
|---|---|---|
| saved | an immutable version exists; its files are readable over HTTP | you: `versioned_artifact_save_version` |
| published | it is the artifact's current version; a launch opens current | you: `versioned_artifact_publish` |
| activated | an operator approved this exact version and its actions | an operator only: `miniapp:activate` |

A version that is not activated still opens, but **every action fails**. You cannot activate.
Every new version needs a new activation.

## Never do these

| Never | Why |
|---|---|
| A server, `server.js`, `package.json`, `npm`, `node_modules`, Express, a build step | Nothing in a version runs on a server. Only the browser page runs. |
| A credential, API key, token, password or env var in any file | A saved version is readable over HTTP. You hold no tool credential. |
| `fetch` or XHR to Jira, Anthropic, any external host or `localhost` | The page gets data only from actions or from data you put in the file. |
| An LLM call when the page runs | No LLM is in the action path. If the app needs AI text, **you** write it now and put it in the file. |
| An external script, stylesheet or CDN; inline `style`; `innerHTML` with data | See the `miniapp-ui` skill. |
| "The app is live", "I tested it", or a preview link given as "the app" | You cannot open the page, and actions need an operator. Say what is done and what is not. |

## Decide before you write

1. **Actions.** Which tools must the page call? Use the exact tool names from your own tool list,
   for example `jira_search` or `jira_add_comment`. A tool that you do not have is not an action.
2. **Data.** Which data is fixed when you build (put it in the file) and which must be live (a read
   action)? AI text, summaries and proposals are always fixed: you write them now.
3. **Writes.** Every write action (comment, transition, create) runs only after a user click.
4. **Panel origin.** The bridge needs the exact address of the panel. Take it from your agent_view
   instructions. If it is not there, ask the user: "What address do you open the Agento panel at?"
   Use its scheme, host and port exactly, for example `https://panel.example.com`. Do not guess.

## Build steps

1. `versioned_artifact_list`. If you already have an artifact for this app, make a new version of
   it. Do not make a second artifact.
2. `versioned_artifact_init` — only for a new app. Use the `artifact_code` in the answer, not the
   one you asked for.
3. `versioned_artifact_get_current`. Keep `current_version`.
4. `versioned_artifact_create_draft` with `base_version: "current"`. Write files in the `path` it
   returns, with your normal file tools.
5. Write exactly two files:
   - `index.html` — the entry file. Markup plus one inline `<script type="module">`, as in the
     `miniapp-ui` page skeleton. No other script or stylesheet file.
   - `miniapp.json` — see below.
6. `versioned_artifact_save_version`. Keep the `version_id`.
7. `versioned_artifact_diff`. Check that the saved version holds what you expect.
8. `versioned_artifact_publish` with `version_id` and `expected_current_version` from step 3. A
   launch opens the current version, so an unpublished version is never launched.
9. `versioned_artifact_discard_draft`.
10. Hand over (see below).

## `miniapp.json`

```json
{ "schema": 1, "title": "Jira comment approver", "actions": ["jira_add_comment"] }
```

- Exactly these three keys. Any other key makes the file invalid.
- `schema` is the number `1`.
- `title` is 1–200 characters.
- `actions` is a list of at most 64 different tool names, each `^[a-z0-9_]{1,64}$`. A tool name,
  never a URL.
- List only the tools the page calls. An empty list is valid: the page then only shows data.

## Call an action

Load the bridge from the kit, at the kit version the `miniapp-ui` skill names:

```js
import { createAgentoSdk } from "/_ui/<kit version>/agento-bridge.js";
const PANEL_ORIGIN = "https://panel.example.com"; // from "Decide before you write", step 4
const AGENT_EMAIL = "agent@example.com"; // your own email from SOUL.md, as when you call the tool
let sdk;
try { sdk = createAgentoSdk({ panelOrigin: PANEL_ORIGIN }); }
catch { status.textContent = "Open this app from the Agento panel."; }

function payload(body) { // the toolbox answer: {ok, result: {content: [{text}], isError}}
  if (!body?.ok || body.result?.isError) return null;
  try { return JSON.parse(body.result.content[0].text); } catch { return body.result.content[0].text; }
}

async function approve(row) { // a button click calls this
  await sdk.ready;
  const { status: code, body } = await sdk.callAction("jira_add_comment", { user: AGENT_EMAIL, issue_key: row.key, body: row.text });
  if (code !== 200 || payload(body) === null) { /* mark the row failed; keep it editable */ }
}
```

- The arguments are the same as when you call that tool yourself. Give every required argument.
  Many tools (all Jira tools) require `user`: your own email from SOUL.md. Write it into the page
  as in the snippet. It is an identity for the log, not a secret.
- `status` other than 200, or `isError`, is a failure. Show it on that row. Do not retry a write
  by itself: let the user click again.
- For a bulk action, call one row at a time (`for … of` with `await`) and mark each row done or
  failed. At most 16 calls can wait at the same time.
- `createAgentoSdk` throws when the page was not opened by the panel. Catch it and say so.

## Put data in the page

Put the data you made into one JSON block in `index.html`:

```html
<script type="application/json" id="data">[{"key":"AI-1","summary":"\u003cb>Fix\u003c/b>","proposal":"Next step: …"}]</script>
```

Read it with `JSON.parse(document.getElementById("data").textContent)`. Show each value with
`textContent`.

**Escape rule.** External text (a Jira summary, an email subject) can contain `</script>`. That
closes the block, and the rest runs as script. So: make the JSON with a JSON encoder, then replace
every `<` with `\u003c` (six characters: backslash, `u`, `0`, `0`, `3`, `c`). The JSON stays valid and parses back to `<`.

**Check before you save:** between `<script type="application/json" id="data">` and its
`</script>`, there is no `<`.

Anyone who can open the version can read this data. Put in no secret, and no data that the user
of the app may not see. New data means a new version.

## Hand over

Tell the user, in their language:

- the `artifact_code` and the `version_id`, and that the version is saved and published;
- that the actions work only after an operator runs these steps (`<view>` is the agent_view code):

```bash
uv run bin/agento miniapp:activate <artifact_code> <version_id>
uv run bin/agento tool:enable miniapp --agent-view <view>
uv run bin/agento tool:enable miniapp_get_launch_spec --agent-view <view>
uv run bin/agento tool:enable miniapp_list --agent-view <view>
uv run bin/agento tool:enable versioned_artifact --agent-view <view>
uv run bin/agento tool:enable versioned_artifact_get_current --agent-view <view>
uv run bin/agento grant:add --role user --tool miniapp_get_launch_spec --agent-view <view>
uv run bin/agento grant:add --role user --tool miniapp_list --agent-view <view>
uv run bin/agento grant:add --role user --tool versioned_artifact_get_current --agent-view <view>
uv run bin/agento grant:add --role user --operation artifact.launch --agent-view <view>
# and for each action in miniapp.json:
uv run bin/agento tool:enable <action> --agent-view <view>
uv run bin/agento grant:add --role user --tool <action> --agent-view <view>
```

- that every new version needs `miniapp:activate` again;
- then: open the panel, **Miniapps → Open**.

Do not say that you tested the app. Do not give the preview address as the app.

## Example: Jira comment approver

The user wants: a list of Jira issues, a short "next step" comment proposed for each, an Approve
button on each row, an "Approve selected" button, comments added one by one.

1. You call `jira_search` yourself and read the issues.
2. You write each proposal now ("Next step: …"). The page calls no LLM.
3. You put `[{key, summary, proposal}]` into the data block, escaped.
4. `miniapp.json`: `"actions": ["jira_add_comment"]`. Add `jira_search` only if the user must
   refresh the list from the page.
5. `index.html`: an `<ag-table>` with a checkbox, the key, the summary and an editable
   `<textarea>` with the proposal per row; an Approve button per row; "Approve selected" calls
   `jira_add_comment` for each checked row, one at a time, and marks each row done or failed.
6. Save, diff, publish, discard the draft, hand over.

## Common mistakes

| Mistake | Do this |
|---|---|
| Wrote `server.js` and `npm start` steps | Remove them. Use actions. |
| Asked the user for `JIRA_TOKEN` or an API key | Never. The toolbox holds credentials; an action uses them. |
| Page calls an LLM API for proposals | Write the proposals yourself and put them in the data block. |
| Stopped after writing files | Files in a draft are not a version: `save_version`, then `publish`. |
| No `miniapp.json` | The panel cannot run actions without it. Write it. |
| Gave the preview link as "the app" | The app opens from the panel, after activation. |
| Said "it works" | Say: saved and published; actions wait for an operator. |
