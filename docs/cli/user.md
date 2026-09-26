# user:*

Manage panel users. The commands call the same functions as the web admin API
(`src/agento/framework/access/accounts.py`). An operator at the CLI needs no panel session and
no actor check.

| Command | Effect |
|---|---|
| `user:create <username> [--role admin\|user]` | Create a user. Without `--role`, choose it with the arrow keys. |
| `user:list` | List users with role and state. |
| `user:set-role <username> <admin\|user>` | Change the role. Ends every session and launch of that user. |
| `user:deactivate <username>` | Stop the user from signing in. Ends every session and launch of that user. |
| `user:activate <username>` | Let the user sign in again. |
| `user:password <username>` | Set a new password. Ends every session of that user. |

A username matches `^[a-z0-9][a-z0-9._-]{0,63}$`. A password has 12 to 1024 characters.

## Passwords never come from argv

`user:create` and `user:password` have no password argument. On a terminal they ask twice with
`getpass`. Otherwise they read stdin to its end (trailing newlines are removed):

```bash
bin/agento user:create admin --role admin
bin/agento user:password admin < /run/secrets/admin-password   # a mode-0600 file
```

Passwords are stored as scrypt hashes (`N=16384, r=8, p=1`). A user created by the admin API
with no password cannot sign in until `user:password` sets one.

See also [grant.md](grant.md) and [../deployment/panel.md](../deployment/panel.md).
