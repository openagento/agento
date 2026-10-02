# Example miniapp: job status

Hand-written HTML over the kit at `/_ui/1.0.0/`. It shows a job card and a sortable table from
data in the page, and it calls one declared action, `miniapp_list`, through the panel.

## Run it

`<view>` is an agent_view code and `<view_id>` its id. Every step is an operator step except the
launch. `config:set` replaces the list: keep any codes the view already has.

```bash
uv run bin/agento module:enable miniapps && uv run bin/agento setup:upgrade
uv run bin/agento artifact:init job-status --source frontend/miniapps/examples/job-status --title "Job status"
uv run bin/agento artifact:list                      # the new version id
uv run bin/agento config:set versioned_artifacts/allowed_artifacts job-status --scope agent_view --scope-id <view_id>
uv run bin/agento tool:enable miniapp --agent-view <view>
uv run bin/agento tool:enable miniapp_get_launch_spec --agent-view <view>
uv run bin/agento tool:enable miniapp_list --agent-view <view>
uv run bin/agento tool:enable versioned_artifact --agent-view <view>
uv run bin/agento tool:enable versioned_artifact_get_current --agent-view <view>
uv run bin/agento grant:add --role user --tool miniapp_get_launch_spec --agent-view <view>
uv run bin/agento grant:add --role user --tool miniapp_list --agent-view <view>
uv run bin/agento grant:add --role user --tool versioned_artifact_get_current --agent-view <view>
uv run bin/agento grant:add --role user --operation artifact.launch --agent-view <view>
uv run bin/agento miniapp:activate job-status <version_id>
```

Then sign in to the panel, open **Miniapps**, and press **Open**. The launch resolves the
artifact's current version through `versioned_artifact_get_current`, so that tool must be enabled
and granted. The **Miniapps** screen lists activated versions only: run `miniapp:activate` first.

`PANEL_ORIGIN` in `index.html` is `https://panel.localhost:8443`. Change it when your panel runs
on another origin: the SDK posts only to that exact origin.
