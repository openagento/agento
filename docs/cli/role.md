# role:*

Manage panel roles. A role is a row in the `role` table: a `code` (the key that `user.role` and
`role_grant.role` hold) and a `label` (the display name). `admin` and `user` are built in and cannot
be deleted. The model is in [../architecture/panel.md](../architecture/panel.md#roles-and-grants).

| Command | Effect |
|---|---|
| `role:list` | List roles with label, user count, number of scopes with a grant, and `built-in`. |
| `role:create <code> --label LABEL` | Create a role. It has no access until it is granted some. |
| `role:delete <code>` | Delete a role and its grants. Refused for a built-in role and for a role that a user has. |

Shortcuts: `ro:li`, `ro:cr`, `ro:de`.

- A code matches `^[a-z][a-z0-9_]{1,15}$` and cannot be changed later. A label has 1 to 64
  characters and is unique. Rename a label in the panel (Users → Roles → the role → Role info).
- A new role is a `user`-like role: the admin operations (`users.manage`, `grants.manage`,
  `config.write`, `admin.read`, `credentials.manage`) stay bound to the code `admin` and cannot
  be granted.
- To delete a role that users have, first move them: `user:set-role <username> <other role>`.

```bash
bin/agento role:create support --label "Support desk"
bin/agento grant:add --role support --tool jira_add_comment --agent-view dev_01
bin/agento user:set-role alice support
bin/agento role:list
```

See also [user.md](user.md) and [grant.md](grant.md).
