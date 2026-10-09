# Panel, sessions, launches and RBAC (E2)

`web` (`src/agento/web/`) serves the panel API. `proxy` puts it on the panel origin and asks it,
per file request, whether a launched artifact may be served on the apps origin. All logic over
the `role`, `user`, `session`, `launch` and `role_grant` tables is in `src/agento/framework/access/`;
the web API and the `user:*` / `role:*` / `grant:*` CLI call the same functions. Operator page:
[../deployment/panel.md](../deployment/panel.md).

## Origins

| Origin | Serves | Why it is separate |
|---|---|---|
| panel (`AGENTO_PANEL_HOST`) | `/api/*` and `/health` from `web`; `/internal/*` answers 404; every other path is the built panel, static from `proxy` (E8, [../development/frontend.md](../development/frontend.md)) | agent-written code never runs here |
| apps (`AGENTO_APPS_HOST`) | `/a/<code>/v/<id>/…` after `forward_auth`, `POST /launch`, and the miniapp kit at `/_ui/<version>/` (static, immutable, no data) | a page here cannot read panel responses, DOM or the panel cookie |

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

## Session

`POST /api/session` (login) and `GET /api/session` answer
`{user, csrf_token, expires_at, display}`. `display` is `{date_format, timezone}`: the `admin`
module's `admin/locale/date_format` (`us`, `eu` or `iso`) and `admin/locale/timezone` (`browser`
or an IANA zone), resolved at the default scope; a value that is not an option gets the
`config.json` default. It is `null` when the `admin` module is disabled
([admin.md](../modules/admin.md)). The panel formats every `Timestamp` with it.

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
   |                               | retention lock (va_ret:<sha1(code)>, 5 s) taken
   |                               | versioned_artifact_get_current via the toolbox
   |                               |   (a user_session capability, one call)
   |                               | miniapp_get_launch_spec for that version (E6)
   |                               | create_launch: user row locked, grant re-checked,
   |                               |   cap applied, spec pinned, only hashes stored
   |                               | retention lock released
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
   |                               | proxy secret? path from X-Forwarded-Uri parsed once;
   |                               |   launch live, redeemed, (code, version) match,
   |                               |   user active? → 200 + X-Agento-Upstream-Path, else 403
   | POST /api/launches/<id>/actions/<tool> {arguments}   (E6, from the bridge)
   |------------------------------>| launch yours, redeemed, live? else 404
   |                               | tool in allowed_actions? else 403
   |                               | one miniapp capability (ceiling = allowed_actions)
   |                               |   → POST /internal/tools/<tool>:invoke
   |<------------------------------| the toolbox answer
```

- The exchange code travels in a form body, never in a URL, so it is not in history, in a
  `Referer` or in a log, and a prefetch or a scanner cannot consume it. `GET /launch` is 404.
- `current` resolves only at launch time. The apps origin serves only immutable
  `/a/<code>/v/<version id>/` paths; there is no `current` route. `web` parses the raw path once
  and tells `proxy` exactly which upstream path to fetch (PRD E6 §6.2).
- The retention lock is the one the toolbox prune takes, so a launch never pins a version the
  prune is deleting ([../modules/versioned-artifacts.md](../modules/versioned-artifacts.md)).
  Busy for 5 s: `503`.
- The redeem does not need the proxy secret: the exchange code is the credential. It needs the exact panel `Origin`. It does not check `Sec-Fetch-Site`: panel and apps can be two sites (`panel.localhost` and `apps.localhost` are), so the browser sends `cross-site`.
- A 403 from `/internal/authz/app` clears every presented launch cookie whose launch is no longer
  live. Caddy returns the deny response, headers included, to the client (measured).
- A version that is not an activated miniapp gets the no-manifest constants in `launch`:
  `manifest_fingerprint = sha256("")`, `allowed_actions = []` — it serves files and authorizes no
  action. An activated one pins its fingerprint and actions; see
  [../modules/miniapps.md](../modules/miniapps.md). A toolbox failure while reading the spec is
  `503`, never a files-only guess.
- `GET /api/agent-views/<id>/miniapps` lists the activated miniapps the user may launch there. A reachable view without `artifact.launch` answers an empty list, not 404: the view is already in the user's list.

## Roles and grants

Roles are rows in the `role` table: a `code` (`^[a-z][a-z0-9_]{1,15}$`, the key that `user.role`
and `role_grant.role` hold, never renamed) and a unique `label` (the display name). `admin` and
`user` are seeded and built in: they cannot be deleted. `user.role` has a foreign key with no action,
so a role that a user has cannot be deleted; `role_grant.role` cascades, so a deleted role's grants
go with it. Every writer (`create_user`, `update_user`, `add_grant`, `set_role_grants`) checks the
code against `role` inside its transaction, and the foreign key is the backstop.

`admin` is the one code with the built-in admin operations `users.manage`, `grants.manage`,
`config.write`, `admin.read` and `credentials.manage` (`accounts.may`); they are not grantable, so a
new role is a `user`-like role (DECISIONS.md 2026-10-06 Roles are rows). Everything else comes from
`role_grant` rows:

- `grant_kind = 'tool'`: the role may call that tool, if it is enabled there;
- `grant_kind = 'operation'`: an ACL resource — the built-in `artifact.launch`, or one a module
  declares in `di.json` (`"acl_resources": [{"id": "<module>.<name>", "title": "…"}]`, the Magento
  `acl.xml` pattern). `admin` has every module-declared resource built in; `artifact.launch` is a
  grant for every role, `admin` included. The conversation module declares
  `conversation.run_details` (DECISIONS.md D-E9-6).

A row has exactly one scope. A workspace grant reaches the workspace and every view in it. A view
grant reaches only that view. The same SQL rule is used in Python (`accounts._granted`) and in the
toolbox (`src/agento/toolbox/capability.js` `GRANTS_SQL`, handed to every auth source as
`grants`); the fixture
`tests/fixtures/role_grant_v1.json` holds both to it. The toolbox reads the role code from `user`
and needs no list of roles.

### Panel: Users → Roles

All behind `grants.manage`:

| Method | Path | Answer |
|---|---|---|
| GET | `/api/admin/roles` | `[{code, label, builtin, users, scopes}]` — `scopes` counts the scopes with a grant |
| POST | `/api/admin/roles` | `{code, label}` → 201 the role |
| GET | `/api/admin/roles/{code}` | the role plus `scopes: [{workspace_id, agent_view_id, tools, operations}]` |
| PATCH | `/api/admin/roles/{code}` | `{label}` (the code never changes) |
| DELETE | `/api/admin/roles/{code}` | 204; 400 with the reason (built in, or "3 users have this role") |
| GET | `/api/admin/roles/{code}/resources?scope=&scope_id=` | the tree, below |
| PUT | `/api/admin/roles/{code}/resources` | `{scope, scope_id, tools[], operations[]}` → `{added, removed}` (counts) |

`scope` is `workspace` or `agent_view`; a grant has no default scope (400). An unknown role or scope
id is 404.

The role page shows the role's resources at one scope as a checkbox tree (Magento *Role
Resources*). `GET …/resources` answers `{operations: [{id, title, granted, inherited, builtin}],
toolsets: [{toolset, tools: [{name, enabled, granted, inherited}]}]}`: `granted` is a row at exactly
this scope; `inherited` (agent_view scope only) is a row at the view's workspace, which already
reaches the view; `builtin` is `admin` with a module-declared resource; `enabled` is the tool's
effective `is_enabled` at the scope — a grant does not enable a tool. The tools are those of the
enabled modules (the Tools screen's list).

`PUT …/resources` (`accounts.set_role_grants`) makes the role's rows at **exactly that scope** the
given names, in one transaction under the `agento.role_grant` named lock, with the role's users and
the actor locked in id order. It validates like `grant:add` (declared tools, grantable operations,
at most 1000 names), inserts the added names in one statement, and never inserts at a view a name
that the view's workspace already grants (a redundant view row that is already there stays). A row
for a tool of a disabled module is outside the tree, so a PUT keeps it. When anything is removed,
the role's launches in that scope end, as `grant:remove` does. Rows at other scopes are untouched.

Grants are **per role**, not per user (the PRD asks for per-user visibility; see DECISIONS.md).
A user who is not `admin` sees the agent_views its role's grants reach. A scope that it cannot reach answers 404,
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

## Module routes

The route table is composed once, at `web` startup: the built-ins in `api.ROUTES` plus
every enabled module's `routes` declared in its `di.json`. There is **one** dispatch path,
so a module route gets the same auth, CSRF and rate-limit treatment as a built-in — it
cannot opt out of any of them by being declared in a manifest instead of in code.

```json
{"routes": [{"method": "POST", "path": "/api/conversation/messages",
             "handler": "src.routes.send", "json_body": true}]}
```

Rules, enforced by `module:validate` before installation and again by the registry at
startup (one implementation, `framework/route_rules.py`):

- a module owns `/api/<its own module name>/` and nothing else, which is also what keeps it
  off every built-in path and away from any other module's;
- the method is one of GET/HEAD/POST/PUT/PATCH/DELETE, and the handler is a dotted path
  **inside the module**;
- `auth` is always `session`. `auth: "login"` is the sign-in route's own exemption, not a
  manifest switch: a module that could set it could publish an unauthenticated endpoint;
- one manifest may not declare the same method-and-path twice.

The module set is the framework's **one** discovery path
(`module_discovery.module_dirs_by_name`), not a list of roots `web` keeps for itself: core, then
local `app/code` which may override it, then the container-mounted PyPI extensions, which never
shadow a name already present. `web` therefore sees exactly what `bootstrap`, `setup:upgrade` and
`module:validate` see — an installed extension whose routes answer 404 is that list drifting.
`scan_all_modules` (parsed manifests) is derived from the same `module_dirs_by_name`
selection, so a local module that overrides a core one is the copy every consumer reads:
the admin screens, `config:*`, `module:list` and the tool-requirement gate included. A
directory without a `module.json` is not a module to any of them.

Loading the registry **never calls `bootstrap()` and never decrypts anything**: `di.json`
and `modules.json` are already mounted read-only, and `web` shares a network with
`sandbox`, so holding a module's secrets to serve a URL would be blast radius bought for
nothing. There is nothing to clear on reload either — the table is composed once, and a
module's routes appear or disappear when `web` restarts, which `module:enable` already does.

The same pass registers each enabled module's `job_types` from that `di.json`. A route that
publishes a job (a conversation message submit) resolves its type in the `web` process, and
without `bootstrap()` nothing else registers it — the submit would fail with `JobTypeUnknown`.

### Module screens

A core module may also ship `panel/index.ts`, its screens in the panel app. The build collects
them; the panel shows a module's screens only while its `availability.probe` (a GET under its own
`/api/<module>/`) answers 2xx, so a disabled module's screens are hidden. A user or PyPI module
never ships panel JavaScript. See [../development/frontend.md](../development/frontend.md#adding-a-module-screen).

## Admin screens

The admin TUI's screens, for an admin in the panel ([frontend.md](../development/frontend.md#admin-screens)).
The handlers are in `src/agento/web/admin_api.py` and read through `framework/admin/data.py`, the
TUI's read layer. Every route needs a session and its operation; a `user` gets 403. Writes pass
the CSRF controls above. A scope is `?scope=default|workspace|agent_view&scope_id=N` on a GET, and
`{scope, scope_id}` in a body; a bad one is 400, and an id that does not exist is 404.

| Method | Path | Operation | Answer |
|---|---|---|---|
| GET | `/api/admin/scopes` | `admin.read` | workspaces and agent_views, for the scope picker |
| GET | `/api/admin/dashboard` | `admin.read` | health, version, module count, recent jobs, credentials, agent views |
| GET | `/api/admin/jobs[?status=]` | `admin.read` | the newest 50 jobs; status `TODO`, `RUNNING`, `SUCCESS`, `FAILED` or `DEAD` |
| GET | `/api/admin/jobs/<id>` | `admin.read` | the job, with prompt, output, summary and error cut to 500 chars |
| GET | `/api/admin/agents` | `admin.read` | agent_views with workspace, ingress count and last build status |
| GET | `/api/admin/credentials` | `admin.read` | credentials with 24 h usage, `type`, `limits` and `limits_at`; `status` and `error_source`, never the error message or the token |
| POST | `/api/admin/credentials/<id>/clear-error` | `credentials.manage` | 204 |
| POST | `/api/admin/credentials/<id>/disable` | `credentials.manage` | 204 (the same as `credential:deregister`) |
| POST | `/api/admin/credentials/<id>/login` | `credentials.manage` | 201 `{id}`; 404 no credential; 400 the scope declares no `interactive_oauth` mode (a harness without `start_web_login` gets 201, then `failed: unsupported`); 409 disabled, not `oauth`, or a login already active |
| GET | `/api/admin/credential-logins/<id>` | `credentials.manage` | `{status, verify_url, user_code, needs_code, error_code, expires_at}`; 404 no login |
| POST | `/api/admin/credential-logins/<id>/code` | `credentials.manage` | `{code}`, 1–300 printable ASCII characters, no space; 204; 400 a bad code; 409 not `waiting`, no code expected, or a code already sent |
| POST | `/api/admin/credential-logins/<id>/cancel` | `credentials.manage` | 204; 404 no active login |
| GET | `/api/admin/tools` | `admin.read` | tools per toolset with `enabled`, `explicit_here`, `blocked_by` |
| GET | `/api/admin/skills` | `admin.read` | skills with `enabled`, `explicit_here` |
| GET | `/api/admin/config/modules` | `admin.read` | modules with config, and their tools that have fields |
| GET | `/api/admin/config?module=` | `admin.read` | the module's fields: source, editable, options, tester; `secret` and `is_set`, and `value` only when not secret |
| PUT | `/api/admin/config` | `config.write` | `{path, reset}`; a secret path is refused |
| DELETE | `/api/admin/config` | `config.write` | 204; a secret path is refused, no override is 404 |
| POST | `/api/admin/config/test` | `config.write` | `{status, code, message}`; an `error` has a fixed message per code |

**Never a secret, never the decryptor.** `web` holds the encryption key (D-BACKEND-1), but the
admin API never sends a secret value. A secret field (`obscure`
or `toolbox_only`, `config_schema.is_secret_field`) is reported by presence only, and the read path
calls no decryptor: credentials are listed without their payload. A tester's `error` and a
credential's error text do not reach the browser, because both can name an internal host or quote
CLI output. See DECISIONS.md 2026-10-02 (D-PANEL-ADMIN-1, D-PANEL-ADMIN-2).

**Re-login.** `POST …/login` only writes a `pending` `credential_login` row (15 minutes to
live). The `credential:web-login` worker in cron claims it, runs the vendor CLI in a PTY and moves
it through `starting` → `waiting` → `verifying` → `done`, or to `failed` with an `error_code`
(`cli_failed`, `bad_url`, `busy`, `disabled`, `unsupported`, `expired`, `abandoned`) or
`cancelled`. The panel polls the GET. When `needs_code` is true (Claude), `web` encrypts the pasted
code with `AGENTO_ENCRYPTION_KEY` and stores only the encrypted bytes; no answer ever carries
`code_box`. See
[credentials.md](../cli/credentials.md#re-login-from-the-panel) and DECISIONS.md 2026-10-09
(D-BACKEND-1).

**Limits.** `limits` is what `credential:limits` last stored: `{windows: [{label, used_pct,
resets_at}], balance_usd}`, or `null`; `limits_at` is when it was last checked. `null` with a
`limits_at` is a failed check (the panel shows "Check failed"); `null` without one has nothing to
show ("—"). `web`
calls no vendor ([credentials.md](../cli/credentials.md#usage-limits)).

**What stays in the TUI**: [docs/cli/admin.md](../cli/admin.md). `web` caches the module schemas
for its process life, like the module routes: restart `web` after `module:enable` or
`module:disable`.

## Rate limiting

Every request `web` serves passes through one limiter, mounted **before route dispatch**, so a
route cannot escape it by omission — a request to a path that does not exist is counted too.
`/health` is the single exemption, named in `rate_limit.EXEMPT_PATHS` rather than claimed by a
route. A per-endpoint policy (the login throttle) is an addition on top, never a replacement,
and the toolbox's own Node limiter is untouched.

The counters live in the `limit_bucket` table, not in the process: an in-process counter limits
one replica each, so the real limit would be the configured number times the number of
replicas. Each increment is one `INSERT … ON DUPLICATE KEY UPDATE` that resets a stale window
and increments in the same round trip, so two concurrent connections at one bucket lose no
increment.

A request is counted on the buckets the listener gives the limiter — **the private ones
always, the shared one only when the caller proved no identity**:

| Bucket | Key | Counted against |
| --- | --- | --- |
| `address` | sha256 of the peer address | `core/limits/max_requests_per_address` |
| `session` | sha256 of the session cookie | `core/limits/max_requests_per_user` |
| `launch` | sha256 of the launch cookie | `core/limits/max_requests_per_user` |
| `user` | sha256 of the user id, added once the session resolves | `core/limits/max_requests_per_user` |

A key is always a hash, never the credential, and never a username-plus-address composite. Two
buckets are what makes evasion pointless in both directions: rotating the address does not
evade the user bucket, and rotating the session token does not evade the address bucket.

The `address` row is **not** touched by an authenticated request. SEC-12: "the address limit
counts failures only, so one caller cannot throttle the others' authorized traffic" — behind one
proxy every caller shares that row, so counting authorized traffic on it let one signed-in caller
spend the whole shared budget and have every stranger behind that address refused, another user's
sign-in included. `web` counts it in the same step that applies its refusal, after the identity is
resolved and still before any handler runs.

`core/limits/auth_failures_before_hold` failed authentications place a hold of
`core/limits/hold_seconds` on the bucket. A hold on a **shared** bucket (`address`, and the
fallback it stands in for today) stops only unauthenticated and failed-authentication requests:
behind one proxy every caller shares that bucket, and holding an authenticated caller for a
stranger's failures would let one caller lock out every other one. A hold on a caller's own
bucket stops that caller either way.

**"Unauthenticated" means no identity was PROVED, not "presented no credential."** The limiter
runs before the route, so it cannot yet know who is calling; it therefore reports a shared refusal
rather than answering it, and `web` resolves the caller — a live session — and applies that
refusal **before dispatch**, to any request that proved nothing. Before
dispatch and not at the response: a refusal written over a finished answer has already derived the
password and already spent the single-use launch code. A route whose *purpose* is to authenticate
(the sign-in route, the launch redemption) is never authenticated by the session cookie it happens
to carry — otherwise one account would be enough to guess other users' passwords with the address
counter silent. Reading a cookie's mere presence as authentication would give every held address a
one-header bypass — a bogus cookie is exactly what a brute-force attempt carries.

**The proxy's secret proves the hop, not the caller.** A valid `X-Agento-Proxy-Auth` says the
request came through `proxy`; it says nothing about who sits behind it, so it is not an identity
and buys no exemption. The `forward_auth` subrequest on `/internal/authz/*` is its own case: the
proxy makes one for **every** request it forwards, so counting them would spend the shared ceiling
on ordinary browsing — it therefore **reads** the shared hold and counts nothing. What it does
count is failure: a launch cookie presented and rejected is a failed authentication on the shared
bucket, whichever authorization path answered, so rotating launch cookies buys a fresh private
bucket per attempt and no extra guesses. A subrequest carrying **no** launch cookie is not a
guess and is not counted — that is a visitor without a launch, not an attempt at one.

A failed authentication that cannot be **counted** is answered `503`, not `401`: the counter is
what bounds the guesses, and answering the 401 anyway is an unbounded number of free ones. The
limiter fails closed on every path — an unavailable counter, an uncountable failure.

Every counter, hold and prune is **committed by the limiter itself**. Request connections are
`autocommit=False` and `web` closes them without committing, so a count left in the transaction is
a count rolled back — and a limiter that keeps nothing is not a limiter.

Each row's `expires_at` is `max(window_end, held_until) + core/limits/bucket_retention_seconds`,
recomputed by every write, and `limits:prune` (cron, every 15 minutes) deletes what has expired —
so a flood of distinct addresses cannot grow the table without bound. The retention is a floor on
the sweep interval, never a cap on a live row's life.

A limiter that cannot count is not a limiter: if the query fails, the request is refused with
`503` (SEC-12, fail closed). A refusal answers `429` with `Retry-After`.

## Streaming responses

A handler may return `StreamingResponse` (`web/streaming.py`) instead of `Response`. The listener
then writes each frame as the generator yields it, and five properties hold.

**No `Content-Length`.** The length is unknown when the headers go out. uvicorn sends the frames
as HTTP/1.1 chunks (to an HTTP/1.0 client, up to the close), so the body ends where the generator
ends. It also carries `X-Accel-Buffering: no`, so nothing between `web` and the reader collapses the stream
into one reply.

**Frames are flushed as they are produced.** Every frame is written and flushed in turn, so the
first one is readable by the client while the handler is still running. That is the whole point,
and it is asserted against a real socket rather than a fake writer.

**A disconnect ends the loop.** uvicorn drops a write to a closed socket silently, so the listener
watches the ASGI `http.disconnect` message and stops after the `next()` in progress.

**Streams have their own thread budget.** The generator is sync and waits between polls, so the
listener calls `next()` on a thread from `server.MAX_STREAMS`, never from the request pool. Open
streams cannot stop other requests.

**The generator's `finally` releases the §7.3 stream slot, and the listener closes the generator
on every exit** — a clean end, a disconnect, and a handler that raised mid-stream. The close is
explicit, not left to the garbage collector: a slot released only because CPython happened to drop
the last reference is not released. A handler that raises after the headers are out gets no `500`
— there is none left to send — so the stream is cut and the failure is written to stderr with the
other request failures.

Heartbeats are ordinary frames from the same generator. A second writer would need its own lock,
its own disconnect handling and its own share of the `finally`.

**A streaming route is an ordinary route.** Auth, CSRF and the rate limiter run before the handler
is called, exactly as for a materialized response — an unauthenticated stream is `401` and a
throttled one is `429`, and in neither case is the handler reached. The return type changes what
is written, never the gate in front of it.

`sse()` and `frame()` in the same module build `text/event-stream` bodies. A frame's `id:` is what
makes replay automatic: the browser resends the last one it saw as `Last-Event-ID` on reconnect
(WHATWG HTML §9.2.3), so the cursor needs no client code. A multi-line body is sent as one `data:`
line per line, because a raw newline would end the frame early.

## Contract deviations

Recorded in [DECISIONS.md](../../DECISIONS.md) (2026-09-25, E2): the checker receives the
capability's scope; visibility is per role; the exchange code is a POST field, not a query
parameter; no manifest seam in E2; `current` resolves through the toolbox.
