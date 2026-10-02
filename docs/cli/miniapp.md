# miniapp:activate / miniapp:deactivate / miniapp:list

Let one artifact version run as a miniapp, stop it, or list what is activated. Module:
[miniapps](../modules/miniapps.md).

```bash
uv run bin/agento miniapp:activate <artifact_code> <version_id> [--actions a,b] [--actor <who>]
uv run bin/agento miniapp:deactivate <artifact_code> <version_id> [--actor <who>]
uv run bin/agento miniapp:list
```

Shortcuts: `mi:ac`, `mi:de`, `mi:li`.

| Flag | Meaning |
|---|---|
| `--actions a,b` | The actions launches of this version may call. Each must be in the version's `miniapp.json`. Default: every action in it |
| `--actor <who>` | Recorded in the audit row (default `admin`) |

`activate` reads the version's `miniapp.json`, checks it, and stores its fingerprint with the
actions. Running it again replaces the row. A launch made **before** a change keeps what it
pinned; a launch whose pinned fingerprint no longer matches an activation can call no action.

`deactivate` removes the row: every live launch of that version stops reaching its actions at
the next call. Its files stay reachable under the launch.

**There is no tool equivalent.** An agent must not decide what a user's browser may call.

## How it runs

Host-local (`_LOCAL_MODULE_COMMANDS`), exec'ing into the toolbox with
`node …/miniapps/toolbox/cli.js --op activate|deactivate|list`, like `artifact:*`.

## Failures

Failures print as `Error: <ERROR_CODE>: <message>` and exit non-zero — never a traceback.

| Code | Cause |
|---|---|
| `MANIFEST_INVALID` | The version has no valid `miniapp.json` |
| `ACTION_NOT_DECLARED` | An `--actions` entry is not in the manifest |
| `NOT_ACTIVATED` | `deactivate` of a version that is not activated |
| `FILE_TOO_LARGE` | `miniapp.json` is over 64 KiB |
| `MINIAPP_STORE_UNAVAILABLE` | The toolbox has no database connection |
| `INVALID_PATH` | A bad artifact code or version id |
| `ARTIFACT_NOT_FOUND` / `VERSION_NOT_FOUND` | No such artifact or version |
