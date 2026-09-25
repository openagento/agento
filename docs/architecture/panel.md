# Panel, sessions, launches and RBAC (E2)

`web` (`src/agento/web/`) serves the panel API. `proxy` puts it on the panel origin and asks it,
per file request, whether a launched artifact may be served on the apps origin. All logic over
the `user`, `session`, `launch` and `role_grant` tables is in `src/agento/framework/access/`;
the web API and the `user:*` / `grant:*` CLI call the same functions. Operator page:
[../deployment/panel.md](../deployment/panel.md).

## Origins

| Origin | Serves | Why it is separate |
|---|---|---|
| panel (`AGENTO_PANEL_HOST`) | UI, `/api/*`; `/internal/*` answers 404 | agent-written code never runs here |
| apps (`AGENTO_APPS_HOST`) | `/a/<code>/v/<id>/…` after `forward_auth`, and `POST /launch` | a page here cannot read panel responses, DOM or the panel cookie |

The split stops cross-origin **reads**. It does not stop writes: panel and apps are same-site,
so a page on apps can make the browser send a credentialed request to the panel. The CSRF controls
below close that.

## Cookies

| Cookie | Attributes | Holds |
|---|---|---|
| `__Host-agento-session` | `Secure; HttpOnly; SameSite=Strict; Path=/`, no `Domain` | the session token (the DB has its SHA-256) |
| `__Host-agento-launch-<launch id>` | `Secure; HttpOnly; SameSite=Lax; Path=/`, no `Domain` | one launch token (the DB has its SHA-256) |

`__Host-` makes each cookie host-only, so the panel cookie never goes to the apps host and the
reverse. Each launch has its own cookie name, so two tabs with two versions of one artifact do not
overwrite each other: the file check accepts the request when **any** live launch cookie matches the
path's `(code, version)`. At most 20 launch cookies are read from one request; the same 20 are checked and cleared, and any
others are ignored.

## CSRF controls

Every `POST`, `PUT`, `PATCH` and `DELETE` on `/api/*` needs all of these:

1. `Origin` equal to the exact panel origin;
2. `Sec-Fetch-Site: same-origin` and `Sec-Fetch-Mode` `cors` or `same-origin`, when the browser
   sends them (a navigation or a form post is refused);
3. `X-CSRF-Token` equal to HMAC-SHA256(key = session token, `agento-csrf`). The login and
   `GET /api/session` responses return it. A page that cannot read the HttpOnly cookie cannot
   compute it, and it is not stored.

`OPTIONS` answers 405 everywhere and no response has `Access-Control-Allow-*`: there is no
credentialed CORS.

## Launch exchange

```
panel page                      web                                  proxy / apps origin
   | POST /api/launches {agent_view_id, artifact_code}
   |------------------------------>| can_reach + artifact.launch? else 404
   |                               | versioned_artifact_get_current via the toolbox
   |                               |   (a user_session capability, one call)
   |                               | create_launch: user row locked, grant re-checked,
   |                               |   cap applied, only hashes stored
   |<------------------------------| 201 {launch_id, version_id, redeem: {url, fields}}
   | form POST (target=_blank) to https://apps…/launch, fields launch_id + code
   |---------------------------------------------------------------------> rewrite to
   |                               |<-------------------------------------| /internal/launch/redeem
   |                               | Origin = panel? one atomic UPDATE wins (30 s, once)
   |<---------------------------------------------------------------------| 303 /a/<code>/v/<id>/
   |                                                                      | + launch cookie
   | GET /a/<code>/v/<id>/index.html (cookie)
   |---------------------------------------------------------------------> forward_auth
   |                               |<-------------------------------------| /internal/authz/app
   |                               | proxy secret? launch live, redeemed, (code, version) match,
   |                               |   user active? → 200, else 403
```

- The exchange code travels in a form body, never in a URL, so it is not in history, in a
  `Referer` or in a log, and a prefetch or a scanner cannot consume it. `GET /launch` is 404.
- `current` resolves only at launch time. The apps origin serves only immutable
  `/v/<version id>/` paths.
- The redeem does not need the proxy secret: the exchange code is the credential.
- A 403 from `/internal/authz/app` clears every presented launch cookie whose launch is no longer
  live. Caddy returns the deny response, headers included, to the client (measured).
- E2 writes the no-manifest constants into `launch`: `manifest_fingerprint = sha256("")`,
  `allowed_actions = []`. An E2 launch serves files and authorizes no action. E6 owns the manifest.

## Roles and grants

Two roles: `admin` and `user`. `admin` has the built-in operations `users.manage`,
`grants.manage` and `config.write`. Everything else comes from `role_grant` rows:

- `grant_kind = 'tool'`: the role may call that tool, if it is enabled there;
- `grant_kind = 'operation'`: the one grantable operation is `artifact.launch`.

A row has exactly one scope. A workspace grant reaches the workspace and every view in it. A view
grant reaches only that view. The same SQL rule is used in Python (`accounts._granted`) and in the
toolbox checker (`modules/web/toolbox/auth-sources.js` `GRANTS_SQL`); the fixture
`tests/fixtures/role_grant_v1.json` holds both to it.

Grants are **per role**, not per user (the PRD asks for per-user visibility; see DECISIONS.md).
A `user` sees the agent_views its role's grants reach. A scope that it cannot reach answers 404,
not 403.

## Per-call evaluation

A panel tool call (`POST /api/tools/{name}:invoke`) mints one single-use `user_session`
capability for that call. Its lifetime is at most `core/auth/capability_ttl` and never more than
what is left of the session. The toolbox then checks the capability and calls the `session`
checker, which re-reads the session and the user and computes the role's permitted tools **for
the capability's scope**. Then it checks `is_enabled` for that scope. So:

- a disabled tool is refused for every role, `admin` included (there is no second allow-list);
- `user:set-role`, `user:deactivate` and `user:password` revoke the user's sessions (and, except
  for a password change, launches) in the same transaction;
- `grant:remove` revokes that role's launches in the grant's scope;
- every access write takes its `user` row locks in one statement, in id order, and checks the
  acting admin again inside the transaction. `create_launch` locks the user row too, so a launch
  sees either the old access (and is revoked with it) or the new one.

## Contract deviations

Recorded in [DECISIONS.md](../../DECISIONS.md) (2026-09-25, E2): the checker receives the
capability's scope; visibility is per role; the exchange code is a POST field, not a query
parameter; no manifest seam in E2; `current` resolves through the toolbox.
