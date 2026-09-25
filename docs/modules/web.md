# web

Core module that owns the panel's configuration and the toolbox `session` auth source. The
service code is in `src/agento/web/` (not in the module), because it runs in its own
container. Design: [../architecture/panel.md](../architecture/panel.md). Operator page:
[../deployment/panel.md](../deployment/panel.md).

| File | Contents |
|---|---|
| `module.json` | no tools (`tools: []`), no `sequence` |
| `config.json` / `system.json` | `web/launch/max_concurrent` |
| `toolbox/auth-sources.js` | `authSources = [["session", checkSession]]` |

## Config

| Path | Default | Scopes | Meaning |
|---|---|---|---|
| `web/launch/max_concurrent` | `5` | default, workspace | Live launches one user may hold; past it the oldest ends. Clamped to 20. |

`web` never runs `bootstrap()`, so it resolves this value itself: ENV
`CONFIG__WEB__LAUNCH__MAX_CONCURRENT` → DB (workspace, then default) → this module's
`config.json`. A value that is not a positive integer makes `POST /api/launches` answer 503.

## The `session` checker

The toolbox calls `checkSession(sourceId, {capability_kind, workspace_id, agent_view_id, query})`
for every call made with a `user_session` capability. It returns `null` unless the session is
live and not revoked, the user is active, the view belongs to the workspace, and the user's role has
at least one tool grant reaching that scope. It returns the role's permitted tools for that scope,
computed with the same SQL as `framework/access/accounts.py`.

## Disabling the module

With `web` disabled, the toolbox has no `session` checker, so every `user_session` capability is
refused and panel tool calls fail closed. Launches still get their limit from `config.json`
(read from disk), and sign-in and the admin API keep working.
