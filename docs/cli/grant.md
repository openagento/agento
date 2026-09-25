# grant:*

Manage role grants: what a role may do, and where. The model is in
[../architecture/panel.md](../architecture/panel.md#roles-and-grants).

| Command | Effect |
|---|---|
| `grant:add --role R (--tool NAME \| --operation OP) (--workspace CODE \| --agent-view CODE)` | Add a grant. Prints its id; adding the same grant again prints the existing id. |
| `grant:list [--role R]` | List grants with id, role, kind, name and scope. |
| `grant:remove <grant_id>` | Remove a grant. Ends the launches of that role in the grant's scope. |

- `--tool` must name a tool that some module declares in `module.json` `tools[]` (enabled or
  not). `--operation` accepts only `artifact.launch`. `admin` has `users.manage`,
  `grants.manage` and `config.write` built in; they are not grants.
- Exactly one scope: a workspace grant reaches the workspace and every agent_view in it; an
  agent_view grant reaches only that view.
- A grant does **not** enable a tool. `tool:enable` is the one switch
  ([tools.md](tools.md)); a disabled tool is refused for every role.

```bash
bin/agento grant:add --role user --tool versioned_artifact_get_current --agent-view dev_01
bin/agento grant:add --role user --operation artifact.launch --workspace it
bin/agento grant:list --role user
bin/agento grant:remove 7
```
