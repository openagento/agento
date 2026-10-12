# Example miniapp: job status

Hand-written HTML over the kit at `/_ui/1.0.0/`. It shows a job card and a sortable table from
data in the page. It declares no action (`"actions": []`), so it calls no tool.

## Run it

`<view>` is an agent_view code and `<view_id>` its id.

Agents create artifacts; there is no host upload. Give an agent in `<view>` the
`versioned_artifact_*` tools and ask it to make an artifact `job-status` from the files in this
folder and publish it.

```bash
uv run bin/agento module:enable miniapps && uv run bin/agento setup:upgrade
uv run bin/agento grant:add --role user --operation artifact.launch --agent-view <view>
uv run bin/agento grant:add --role admin --operation artifact.launch --agent-view <view>
```

Then an admin approves the current version for the view:
`POST /api/miniapps/apps/job-status/views/<view_id>/approval` with
`{"expected_version_id": "<current version>"}` (see [miniapps.md](../../../../docs/modules/miniapps.md)).
Sign in to the panel, open **Miniapps**, and press **Open**.
