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
`{user, csrf_token, expires_at, display, locale}`. `display` is
`{date_format, date_format_explicit, timezone}`: the `admin`
module's `admin/locale/date_format` (`us`, `eu` or `iso`) and `admin/locale/timezone` (`browser`
or an IANA zone), resolved at the default scope; a value that is not an option gets the
`config.json` default. `date_format_explicit` is `true` only when ENV or the database gives a
valid `date_format`; when it is `false`, the panel formats a date by the active language.
`display` is `null` when the `admin` module is disabled
([admin.md](../modules/admin.md)). `web` reads the options and the `config.json` defaults of the two
fields once, at start (`api.display_schema`); a session answer reads only ENV and the database, and
no module file. So a change of the `admin` files applies when `web` restarts. Enablement is not part of the snapshot: `web` reads `app/etc/modules.json` for each session answer (as for `miniapps`), so `module:disable admin` makes `display` `null` at the next request, and `module:enable admin` gives it back when `admin` was enabled at the start. The two
files are read inside the module directory only (SEC-5). When a field is absent or is not a select,
or its `config.json` default is not one of its options, `display` is `null` and `web` logs one line
that does not show the value; `display_schema` raises for no content of these files (the base module loader reads `system.json` before it, with its own rules: [i18n.md](../development/i18n.md#validation-and-failure)). The panel formats every `Timestamp` with it; with `null` it
formats a date by the active panel language, in the browser's time zone.

`user` also has `ui_locale`: the language tag the account stores, or `null`. `locale` is
`{effective, instance, available, version}`:

| Field | Meaning |
|---|---|
| `effective` | the language the panel shows: `user.ui_locale` when the registry has it, else `instance` |
| `instance` | `admin/locale/language` (ENV → default-scope DB row → `config.json`); `en` when the registry does not have the value or `admin` is disabled |
| `available` | `[{tag, name}]`: the registered languages, each with its name in that language |
| `version` | the content hash of the `effective` catalog; the panel reads the catalog again when it changes |

See [Languages](#languages) for the routes and [i18n.md](../development/i18n.md) for the catalogs.

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
   | POST /api/miniapps/launches {agent_view_id, artifact_code}
   |------------------------------>| can_reach + view of the app? else 404
   |                               | execute (approval, artifact.launch, every
   |                               |   manifest tool grant; admin: approval)? else 403
   |                               |   (POST /api/miniapps/previews, edit on the app:
   |                               |    `current`, no action)
   |                               | retention lock (va_ret:<sha1(code)>, 5 s) taken
   |                               | toolbox /admin resolve of the approved version:
   |                               |   served tree restored, content_id equal? else 409
   |                               | create_launch: user row locked, grant re-checked,
   |                               |   cap applied, version and actions pinned, only hashes stored
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
- A launch serves the version approved for the view and pins its `allowed_actions`; a preview
  serves `current` and authorizes no action. See [../modules/miniapps.md](../modules/miniapps.md).
  A toolbox failure is `503`, never a files-only guess.
- The miniapps routes are `acl: "self"` with **instance rights**: each app has `view`, `edit`,
  `remove` and `execute` per user (members, the creator, admin), checked in the handler; no `view`
  is 404, `view` without the right is 403 (DECISIONS.md D-MINIAPP-3).
- `GET /api/miniapps/apps` lists every app the user may view, with its rights and its approved
  views. It is the `miniapps` panel probe (every signed-in user gets 2xx).
- `GET /api/miniapps/apps/<code>/views/<id>/approval` (`edit` on the app, and admin or
  `artifact.launch` in the view) answers the approval (or `null`) and `current` with its manifest
  actions, for the app settings page `/apps/<code>`.

## Roles and grants

Roles are rows in the `role` table: a `code` (`^[a-z][a-z0-9_]{1,15}$`, the key that `user.role`
and `role_grant.role` hold, never renamed) and a unique `label` (the display name). `admin` and
`user` are seeded and built in: they cannot be deleted. `user.role` has a foreign key with no action,
so a role that a user has cannot be deleted; `role_grant.role` cascades, so a deleted role's grants
go with it. Every writer (`create_user`, `update_user`, `add_grant`, `set_role_grants`) checks the
code against `role` inside its transaction, and the foreign key is the backstop.

Access comes from `role_grant` rows:

- `grant_kind = 'tool'`: the role may call that tool, if it is enabled there;
- `grant_kind = 'operation'`: a scoped operation from an `acl.json` `operations` list (see
  [ACL resources](#acl-resources-acljson)) — the framework's `artifact.launch`, or a module's, such
  as `conversation.run_details`. `admin` has every module operation built in; `artifact.launch` is a
  grant for every role, `admin` included (DECISIONS.md D-E9-6);
- `grant_kind = 'operation'` with **no scope**: an admin panel leaf, from an `acl.json` `screens`
  list — one per admin screen (`admin.dashboard`, `admin.jobs`, `admin.agents`, `admin.credentials`,
  `admin.tools`, `admin.skills`, `admin.config`, `admin.users`, `admin.roles`; the modules add
  `versioned_artifacts`) and one per action (`admin.credentials.edit`,
  `admin.config.edit` — also the Tools and Skills switches, `admin.tools.classify` — tool labels
  and review status, which also need the `admin` role, `admin.users.create|edit`,
  `admin.roles.create|edit|delete`, `versioned_artifacts.publish|delete`). Miniapps have no leaf:
  their rights are per app (D-MINIAPP-3).
  `admin` holds every leaf built in. The session's `user.acl` lists the leaves the panel shows. A role
  that is not admin grants only the leaves it holds, and only an `admin` creates, edits or assigns an
  admin account (DECISIONS.md D-ADMIN-ACL-1).

Any other row has exactly one scope. A workspace grant reaches the workspace and every view in it. A view
grant reaches only that view. The same SQL rule is used in Python (`accounts._granted`) and in the
toolbox (`src/agento/toolbox/capability.js` `GRANTS_SQL`, handed to every auth source as
`grants`); the fixture
`tests/fixtures/role_grant_v1.json` holds both to it. The toolbox reads the role code from `user`
and needs no list of roles.

### ACL resources (`acl.json`)

The leaves and the scoped operations are declared in `acl.json` files (the Magento `etc/acl.xml`),
never in Python: `src/agento/framework/access/acl.json` holds the core screens (`admin.*`) and
`artifact.launch`; a module may ship its own `acl.json` next to `module.json`
([acl.json](../modules/README.md#acljson)). `framework/access/acl.py` reads them into one
registry, core first and then the enabled modules by name. A leaf of a disabled module leaves the
role tree and `held_admin`; its `role_grant` rows stay and work again when the module is enabled.
`web` builds the registry once at start, with its route table; a CLI command builds it on first use.

**Every panel route names an `acl`** (DECISIONS.md D-ADMIN-ACL-2), like a Magento controller's
`ADMIN_RESOURCE`. The value is one of:

| `acl` | Who passes `server._run` |
|---|---|
| a leaf id, e.g. `"admin.jobs"` | a signed-in user whose role holds the leaf (`accounts.may`), else 403 |
| a list of leaf ids | a signed-in user whose role holds any one of them |
| `"self"` | any signed-in user; the handler scopes to the user's own rows or to the scope grants (`can_reach`, `artifact.launch`, `conversation.*`) |
| `"anonymous"` | anybody; only for a route with `auth: "login"` (`POST /api/session`, `GET /api/panel/bootstrap`) |

A built-in route (`web/api.py`, `web/admin_api.py`) cannot be written without `acl` (`Route.acl`
has no default). A module route in `di.json` `routes[]` must have `acl`; it may name only its own
leaves, core leaves, or the leaves of a module in its `sequence` (MOD-1), and never `anonymous`.
`module:validate` checks this, and `web` checks it again when it composes the route table: a
missing or unknown `acl`, on any route, stops `web` at start. No handler checks a panel leaf of its
own (a test guards `src/` of every core module). Three paths are outside the route table and keep
their own checks: `/health` (none), `/internal/authz/app` (the proxy secret) and
`/internal/launch/redeem` (the exchange code).

### Panel: Users → Roles

Leaves: `admin.roles` to read, `admin.roles.create`, `admin.roles.edit` (PATCH and PUT resources),
`admin.roles.delete`. The role list (`GET /api/admin/roles`) also answers `admin.users`,
`admin.users.create` and `admin.users.edit`: the Users tab shows and picks a role by its label from it. A role that holds only `admin.roles` opens on the Roles tab.

| Method | Path | Answer |
|---|---|---|
| GET | `/api/admin/roles` | `[{code, label, builtin, users, scopes}]` — `scopes` counts the scopes with a grant |
| POST | `/api/admin/roles` | `{code, label}` → 201 the role |
| GET | `/api/admin/roles/{code}` | the role plus `scopes: [{workspace_id, agent_view_id, tools, operations}]` (both ids `null`: the admin panel) |
| PATCH | `/api/admin/roles/{code}` | `{label}` (the code never changes) |
| DELETE | `/api/admin/roles/{code}` | 204; 400 with the reason (built in, or "3 users have this role") |
| GET | `/api/admin/roles/{code}/resources?scope=&scope_id=` | the tree, below |
| PUT | `/api/admin/roles/{code}/resources` | `{scope, scope_id, tools[], operations[]}`, or `{scope: "global", admin[]}` → `{added, removed}` (counts) |

`scope` is `global` (the admin panel, no `scope_id`), `workspace` or `agent_view`; a grant has no
default scope (400). An unknown role or scope
id is 404.

The role page shows the role's resources at one scope as a checkbox tree (Magento *Role
Resources*). `GET …/resources` answers `{operations: [{id, title, namespace, granted, inherited, builtin}],
toolsets: [{toolset, tools: [{name, enabled, granted, inherited}]}]}`: `granted` is a row at exactly
this scope; `inherited` (agent_view scope only) is a row at the view's workspace, which already
reaches the view; `builtin` is `admin` with a module-declared resource; `enabled` is the tool's
effective `is_enabled` at the scope — a grant does not enable a tool. The tools are those of the
enabled modules (the Tools screen's list). At `scope=global` it answers only
`{admin: [{screen, namespace, leaves: [{id, title, granted, builtin}]}]}`.

`namespace` is the catalog namespace of the titles: the module that declares the resource, or
`panel` for the framework file. A screen has the namespace of its first leaf. The panel reads
`<namespace>:acl.<id>` and `<namespace>:acl.screen.<first leaf id>`; with no such key it shows
`title` and `screen`.

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
- `auth` is always `session`. `auth: "login"` is the exemption of the sign-in route and of the sign-in screen's
  texts ([Languages](#languages)), not a manifest switch: a module that could set it could publish an unauthenticated endpoint;
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
and `modules.json` are already mounted read-only, so serving a URL needs no module secret.
The one other registry `web` loads is the harness registry, for a `local` config tester
(`harness_reloader`, framework/harness/loader.py), which loads no observer and dispatches
no event. There is nothing to clear on reload either — the table is composed once, and a
module's routes appear or disappear when `web` restarts, which `module:enable` already does.

The same pass registers each enabled module's `job_types` from that `di.json`. A route that
publishes a job (a conversation message submit) resolves its type in the `web` process, and
without `bootstrap()` nothing else registers it — the submit would fail with `JobTypeUnknown`.

### Module screens

A core module may also ship `panel/index.ts`, its screens in the panel app. The build collects
them; the panel shows a module's screens only while its `availability.probe` (a GET under its own
`/api/<module>/`) answers 2xx, so a disabled module's screens are hidden. A user or PyPI module
never ships panel JavaScript. See [../development/frontend.md](../development/frontend.md#adding-a-module-screen).

A module may also declare a `jobLink` — `{label, api, to}` — to add one button to a job's Details
drawer (Jobs and a schedule's Last runs). The drawer GETs `api` with `{job}` replaced by the job id;
a 2xx answer with a positive integer `id` shows the button, which opens `to` with `{id}` replaced.
Any other answer shows no button, so a reader who may not see the target never learns it exists.
`assertPanelModule` resolves each template for two ids and refuses it unless the two URLs stay on
the panel origin (an `api` under `/api/<module>/`) and differ in exactly one whole path segment
after that prefix; `\`, `?` and `#` are refused too. The
conversation module uses it for **Open conversation** (`GET /api/conversation/jobs/{id}`).

## Admin screens

The admin TUI's screens, for an admin in the panel ([frontend.md](../development/frontend.md#admin-screens)).
The handlers are in `src/agento/web/admin_api.py` and read through `framework/admin/data.py`, the
TUI's read layer. Every route needs a session and its leaf (`Route.acl`); without it the answer is
403. `GET /api/admin/scopes` opens with any core leaf (`admin.*`). Writes pass
the CSRF controls above. A scope is `?scope=default|workspace|agent_view&scope_id=N` on a GET, and
`{scope, scope_id}` in a body; a bad one is 400, and an id that does not exist is 404.

| Method | Path | Leaf | Answer |
|---|---|---|---|
| GET | `/api/admin/scopes` | any core leaf (`admin.*`) | workspaces and agent_views, for the scope picker |
| GET | `/api/admin/dashboard` | `admin.dashboard` | health, version, module count, recent jobs, credentials, agent views |
| GET | `/api/admin/jobs?page=<n>[&page_size=25\|50\|100][&q=][&sort=][&dir=asc\|desc][&status=]` | `admin.jobs` | one page of jobs, `{rows, total, page, page_size}` ([server grids](../development/frontend.md#server-grids)). `q` matches as a literal, accent- and case-insensitive substring of the text `id type status view reference` (one space between parts, an empty part for no view). `sort` is `id` (default, newest first), `type`, `status`, `view`, `ref`, `created`, `started`, `finished`, `duration`, `input_tokens`, `output_tokens`, `source`, `agent_type` or `error_class`. A bad value is 400; a page past the end is `rows: []` with the real `total`. Without `page`, the newest 50 jobs as an array (deprecated, see ROADMAP.md); status `TODO`, `RUNNING`, `SUCCESS`, `FAILED` or `DEAD` |
| GET | `/api/admin/jobs/<id>` | `admin.jobs` | the job, with prompt, output, summary and error cut to 500 chars |
| GET | `/api/admin/agents` | `admin.agents` | agent_views with workspace, ingress count and last build status |
| GET | `/api/admin/credentials` | `admin.credentials` | credentials with 24 h usage, `type`, `limits` and `limits_at`; `status` and `error_source`, never the error message or the token |
| POST | `/api/admin/credentials/<id>/clear-error` | `admin.credentials.edit` | 204 |
| POST | `/api/admin/credentials/<id>/disable` | `admin.credentials.edit` | 204 (the same as `credential:deregister`) |
| POST | `/api/admin/credentials/<id>/login` | `admin.credentials.edit` | 201 `{id}`; 404 no credential; 400 the scope declares no `interactive_oauth` mode (a harness without `start_web_login` gets 201, then `failed: unsupported`); 409 disabled, not `oauth`, or a login already active |
| GET | `/api/admin/credential-logins/<id>` | `admin.credentials.edit` | `{status, verify_url, user_code, needs_code, error_code, expires_at}`; 404 no login |
| POST | `/api/admin/credential-logins/<id>/code` | `admin.credentials.edit` | `{code}`, 1–300 printable ASCII characters, no space; 204; 400 a bad code; 409 not `waiting`, no code expected, or a code already sent |
| POST | `/api/admin/credential-logins/<id>/cancel` | `admin.credentials.edit` | 204; 404 no active login |
| GET | `/api/admin/tools` | `admin.tools` | tools per toolset with `enabled`, `explicit_here`, `blocked_by`, `requires` (the manifest master or `null`); for the `admin` role only, also `catalog`: `{id, source, labels, reviewed, revision}` or `null` before the first catalog sync |
| PATCH | `/api/admin/tools/<id>/classification` | `admin.tools.classify` + `admin` role | `{revision, labels: [key], new_label?, review?: "reviewed"\|"to_review"\|null}` → the `catalog` object; 409 when `revision` is not the row's; 400 an unknown label key |
| GET | `/api/admin/tool-labels` | `admin.tools` + `admin` role | `[{id, key, name, protected, usage}]`; `usage` counts the present tools that carry the label |
| POST | `/api/admin/tool-labels` | `admin.tools.classify` + `admin` role | `{name}` → 201 the label; 409 the normalized name is taken |
| PATCH | `/api/admin/tool-labels/<id>` | `admin.tools.classify` + `admin` role | `{name}` → the label; the key never changes; 400 a built-in label |
| DELETE | `/api/admin/tool-labels/<id>` | `admin.tools.classify` + `admin` role | 204, with the label's assignments; 400 a built-in label |
| GET | `/api/admin/skills` | `admin.skills` | skills with `enabled`, `explicit_here` |
| GET | `/api/admin/config/modules` | `admin.config` | modules with config, and their tools that have fields |
| GET | `/api/admin/config?module=` | `admin.config` | the module's fields: source, editable, options, tester; `secret` and `is_set`, and `value` only when not secret |
| PUT | `/api/admin/config` | `admin.config.edit` | `{path, reset}`; a secret path is refused |
| DELETE | `/api/admin/config` | `admin.config.edit` | 204; a secret path is refused, no override is 404 |
| POST | `/api/admin/config/test` | `admin.config.edit` | `{status, code, message}`; an `error` has a fixed message per code |

**Tool labels.** The catalog (`framework/tool_catalog.py`) holds one row per module tool. A
tool's effective labels are its manifest defaults plus the admin's `add` rows minus the `remove`
rows; a classification write stores only that diff, in one transaction that locks the tool row
and checks `revision` (optimistic lock: a second admin, or a catalog sync that changed the
contract, gets 409). `new_label` creates a custom label, or reuses the one with the same
normalized name (NFKC, case-folded, trimmed, inner spaces collapsed), and assigns it in the same
transaction. A label's `key` is an ASCII slug of its first name (`-2`, `-3` on a collision) and
never changes. The four built-in labels (`private-data`, `untrusted-content`,
`external-communication`, `stub-in-test`) cannot be renamed or deleted; their assignments can
be edited. At most 200 labels. Editing labels never changes the review status; only `review`
does. These fields and routes need the `admin` role on the server, whatever leaves a role
holds (PRD §4): another role gets 403 and no `catalog` field.

**Run assessment (lethal trifecta).** Two fields are for the `admin` role only, checked on the
server: `catalog` on `GET /api/admin/tools` and `tool_risk` on each run of
`GET /api/conversation/threads/{id}` ([conversation.md](../modules/conversation.md#rest);
never for a `conversation.run_details` grant, never in a message or an event). The editor
warning is computed in the browser from the unsaved draft. The run warning is the run's
assessment, frozen at its job claim (`framework/tool_risk.py`) and stored on `execution.tool_risk`:

```json
{"v": 1, "trifecta": true | "possible" | false,
 "tool_coverage": "complete" | "incomplete" | "error",
 "coverage": "complete" | "incomplete" | "error",
 "direct": "pending" | "recorded",
 "categories": {"private-data": ["…"], "untrusted-content": ["…"], "external-communication": ["…"]},
 "uncovered": ["<direct MCP server>"], "notes": ["modules_changed" | "conditional_tools" | "snapshot_too_large"]}
```

- Input: the view's tools after the toolbox gate (`is_enabled` and `requires`) with their
  effective labels, minus the tools the run's test policy stubs or refuses. `trifecta` is `true`
  when these tools cover the three risk labels, also when coverage is incomplete.
- Two writes. At the execution mint `direct` is `"pending"` and `coverage` is `"incomplete"`
  (or `"error"`). After the workspace is written, the harness reports its direct MCP servers
  ([harness-contract.md](harness-contract.md#direct-mcp-servers-optional)) and one UPDATE sets
  `direct: "recorded"`, `uncovered` and `coverage`: `"error"` when `tool_coverage` is `"error"`;
  else `"incomplete"` when `tool_coverage` is `"incomplete"` or a direct server exists; else
  `"complete"`. When the harness cannot report its servers, the record is `direct: "recorded"`,
  `uncovered: []`, `coverage: "incomplete"`. A failed second write leaves `"pending"`, which the
  panel shows as incomplete. Later label edits change neither write.
- Lists are bounded: 50 names per category, 20 in `uncovered`, then `"…+N"`.
- An assessment error never blocks the run: it is recorded as `trifecta: false`,
  `tool_coverage` and `coverage` `"error"`, and empty lists (`snapshot_too_large` when the label
  snapshot passed its row cap).

Limits:

- A tool whose module marks it `availability: "conditional"` (github and bitbucket without a
  credential, the Playwright passthrough tools) may not be registered at run time. When the
  three labels are covered only with such a tool, `trifecta` is `"possible"` (the panel shows it
  red, "Possible lethal trifecta") and `tool_coverage` is `"incomplete"` (`conditional_tools`).
- A toolbox module whose import or `register` fails is a deployment defect, logged by the
  toolbox. Its tools are not callable, but the assessment counts them, so it can over-warn,
  never miss a trifecta.
- A busy consumer does not reload its bootstrap. It reads the default labels (manifests and
  `config.json` `sensitive` defaults) once per bootstrap. When a module file changed since the
  bootstrap, `tool_coverage` is `"incomplete"` with `modules_changed`.
- The assessment is per job claim. An interactive `agento run` (prepare-run) has no execution
  row, so it has no assessment. Without the `conversation` module nothing stores it.
- A direct MCP server is not assessed: its tools are unknown. The run shows its name in
  `uncovered` ([zero-trust.md](zero-trust.md#direct-mcp-servers)).

**Artifacts.** The artifact admin API is `/api/versioned_artifacts/...` (leaves
`versioned_artifacts`, `versioned_artifacts.publish`, `versioned_artifacts.delete`): list, history, make
current, delete. The `versioned_artifacts` panel shows it at `/artifacts` (list) and
`/artifacts/<code>` (versions with their audit rows, make current, delete after the code is typed). `web` reads and writes the store only through the toolbox `/admin` endpoint, as
`service:web:user:<id>`. A delete revokes the artifact's live launches first. See
[versioned-artifacts.md](../modules/versioned-artifacts.md#panel-administration).

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
`503` and the code `SERVICE_UNAVAILABLE` (SEC-12, fail closed). A refusal answers `429` with
`Retry-After`.

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

## Languages

The panel shows one language per user ([i18n.md](../development/i18n.md)). `web` composes the
catalogs once, at start, from the module set that its routes come from (`i18n.install` in
`compose_routes`), so a language pack that is enabled later is served after a `web` restart.
The snapshot also holds whether the `admin` module is enabled and the `config.json` default of
`admin/locale/language`, so a language read opens no module file after the start. The ENV value
and the database row of the instance language are read on each request.

| Method | Path | Access | Answer |
|---|---|---|---|
| GET | `/api/panel/bootstrap` | anonymous | `{locale, version, messages: {panel: {…}}}`: the instance language and the sign-in texts only |
| GET | `/api/panel/i18n/<tag>` | session (`self`) | `{locale, version, messages, fallback}`; an unregistered tag is 404 |
| PATCH | `/api/profile/locale` | session (`self`), CSRF | `{"ui_locale": "<tag>"}` (no other key) → `{ui_locale, locale}`; a tag that is not registered is 400 `LOCALE_UNSUPPORTED`; an `If-Match` header that is not the served catalog ETag is 409 `CATALOG_CHANGED` |

- **`/api/panel/bootstrap` is the second route with no session** (the first is `POST /api/session`).
  It uses the same code-only exemption, `auth="login"`: no manifest can set it. It answers the nine
  keys of `i18n.LOGIN_KEYS` in the instance language, each with its English text when the language
  lacks it. It gives no module name, no module text, no user data and no config value other than the
  language tag. Its `version` is the hash of these sign-in texts only: a private module text or an
  override of another key does not change any byte of the answer. `web` makes the texts and their
  version once, at start, as one value for each language (`Catalogs.login`); a request only looks it
  up, so its work does not grow with the catalog. It does one database read, and the limiter counts
  it like every other request.
- **`/api/panel/i18n/<tag>`** answers the full catalog of one language: `messages` is
  `{namespace: {key: message}}` in that language, and `fallback` holds the English source of each key
  the language lacks (empty for `en`). It sends `ETag: "<version>"` and
  `Cache-Control: private, max-age=0`; a request with an equal `If-None-Match` gets 304 with no
  body. The 304 has no `Content-Length`: a length on a 304 must be that of the 200 answer, so `web`
  sends none on any 304. The catalog names the enabled modules and their config fields, so it needs
  a session.
  The handler gives only the `ETag`. `Sec12` writes the three security headers of every answer and
  drops a handler's value for them; this route is the only one with `Route.revalidates`, a
  code-only flag that changes `Cache-Control` on its 200 and 304. Its refusals are `no-store`.
- **`PATCH /api/profile/locale`** writes the caller's own account only: the user id comes from the
  session, and the body is exactly `{"ui_locale": "<tag>"}`: any other key (also `version`) is 400
  `INVALID_REQUEST`. There is no route that sets the language of another user. The caller may send
  the request header `If-Match: "<version>"`, the ETag of the catalog it holds for the language it
  asks for. When the header is not exactly the ETag that `web` serves for that language now (a
  `web` restart came between the catalog GET and this request; or the value is weak, a list, `*`
  or empty), the answer is 409 `CATALOG_CHANGED` and the account does not change. A request with
  no `If-Match` is not checked. The proxy sends the header to `web`: it removes only the identity
  headers (`tests/unit/framework/docker/test_proxy_config.py`).

## Error codes

Every refusal of the panel API is `{"error": "<English text>", "code": "<CODE>"}`, and
`"params": {…}` when the message has values. The panel shows a text by `code`: it reads
`<namespace>:error.<CODE>`, then `panel:error.<CODE>`, then the screen's own fallback key. It never
shows `error`: that text is for a log, the CLI and an API client, and it does not change with the
language. A `params` value is a number or a short identifier (a field path, a tool name, a tag),
never a secret and never a text that needs translation.

- `api.error(status, message, code=…, params=…, extra=…)` builds the body. `extra` is module fields
  that go beside `error`, `code` and `params`. A route that gives a coarser module code for a coded
  framework error keeps the detail there: `scheduler` answers an upload refusal with code `upload`,
  `reason` = the `UploadError` code and `params` = its params. `api.refusal(exc)` builds it from
  an exception that has `status`, and may have `code`, `params` and `extra`: `AdminError`
  (`versioned_artifacts`) and `ActivationError` (`miniapps`). Do not pass another exception to it.
- `AccessError` and `ConfigWriteError` have `code` and `params` but no `status`: the framework
  layer that raises them knows no HTTP. The handler that catches one chooses the status and calls
  `api.error`: `api._access_error` for `AccessError` (403 for `not allowed`, 404 for a
  `… not found` text, else 400), and the config handlers in `admin_api.py` for `ConfigWriteError`
  (403 for `not allowed`, else 400). `UploadError` and the conversation `SubmissionError` have
  `status`: the route that catches one calls `api.error` with it (`scheduler`: its `_stage`, with `reason`).
- `tests/unit/test_refusal_contract.py` closes the class: it reads the source of every Python file
  in `web/`, in `framework/access/` and in each core module's `src/`, plus `config_write.py` and
  `uploads.py`, and fails on an `error(…)` call with no `code`, on a refusal exception built
  with no `code`, on a response body with an `error` key that `api.error` did not build, and on a
  `Response(…)` with a literal status of 400 or more. The only such answers are the two with an
  empty body that the router gives outside `/api/`: `server._not_the_proxy` (the proxy's
  authorization question with no proxy secret) and `server._outside_the_api` (an unknown path).
- A module names its own codes in upper snake case and ships their texts as `error.<CODE>` in its
  namespace. A code in the `panel` table below (`NOT_FOUND`, `FORBIDDEN`, `INVALID_REQUEST`, …)
  needs no module text.
- The `scheduler` module shipped its lower-case codes before this contract. They stay as they are.
- `versioned_artifacts` passes the store's own code through, and answers `ARTIFACT_REFUSED` when the
  store gives none.
- The config test (`POST /api/admin/config/test`) answers 200 with `{status, code, message}`. The
  framework's own `error` codes are in the `panel` table with status 200. For an `error` result the
  panel shows the catalog text of the code (the field's module, then `panel`), not `message`. A
  module's tester also sends its own result codes (`MODEL_OK`, `NO_CREDENTIAL`, …); they are not
  in the tables and have no catalog text: the panel shows the probe's `message` for `ok`, `fail`
  and `not_configured`, and one fixed text for an `error` with such a code.

A code with no `error.<CODE>` key in a catalog shows the screen's fallback text.
`tests/unit/test_refusal_contract.py` collects the codes from the Python sources (and the store
codes of `versioned_artifacts`) and fails when a code has no `error.<CODE>` text in English and
in Polish, in the catalog of its module or in `panel`. The two codes that only the sign-in page
shows (`AUTH_INVALID_CREDENTIALS`, `AUTH_THROTTLED`) have their texts under `panel:login.error.`.

**`panel`**

| Code | Status | Params | Where |
|---|---|---|---|
| `AUTH_REQUIRED` | 401 |  | any session route |
| `FORBIDDEN` | 403 |  | write guard, CSRF, ACL, a demoted actor |
| `NOT_FOUND` | 404 |  | any route |
| `METHOD_NOT_ALLOWED` | 405 |  | any route |
| `RATE_LIMITED` | 429 |  | limiter (`Retry-After` header) |
| `SERVICE_UNAVAILABLE` | 503 |  | limiter that cannot count (every path of `web`) |
| `INTERNAL_ERROR` | 500 |  | any handler |
| `INVALID_REQUEST` | 400 |  | every malformed client request |
| `BODY_TOO_LARGE` | 413 |  | request body cap |
| `AUTH_INVALID_CREDENTIALS` | 401 |  | `POST /api/session` |
| `AUTH_THROTTLED` | 429 |  | `POST /api/session` |
| `LOCALE_UNSUPPORTED` | 400 | `ui_locale` | `PATCH /api/profile/locale` |
| `CATALOG_CHANGED` | 409 |  | `PATCH /api/profile/locale` with an `If-Match` that is not the served catalog ETag |
| `USERNAME_INVALID` | 400 |  | users |
| `USERNAME_TAKEN` | 400 |  | users |
| `USER_NOT_FOUND` | 404 |  | users |
| `PASSWORD_INVALID` | 400 | `min`, `max` | users |
| `ROLE_UNKNOWN` | 400 |  | users, grants |
| `ROLE_NOT_FOUND` | 404 |  | roles |
| `ROLE_CODE_INVALID` | 400 |  | roles |
| `ROLE_CODE_TAKEN` | 400 |  | roles |
| `ROLE_LABEL_INVALID` | 400 |  | roles |
| `ROLE_LABEL_TAKEN` | 400 |  | roles |
| `ROLE_IN_USE` | 400 | `count` | roles |
| `ROLE_BUILTIN` | 400 |  | roles |
| `GRANT_NOT_FOUND` | 404 |  | grants (CLI) |
| `GRANT_SCOPE_INVALID` | 400 | `name` | grants (CLI) |
| `GRANT_TARGET_UNKNOWN` | 400 |  | grants |
| `GRANT_TOO_MANY` | 400 | `max` | role resources |
| `OPERATION_UNKNOWN` | 400 |  | role resources |
| `TOOL_UNKNOWN` | 400 | `tool` | role resources, config |
| `SKILL_UNKNOWN` | 400 | `skill` | config |
| `AGENT_VIEW_NOT_FOUND` | 404 |  | role resources |
| `BUSY` | 400 |  | grants |
| `CONFIG_PATH_INVALID` | 400 | `path` | config |
| `CONFIG_MODULE_UNKNOWN` | 400 | `module` | config |
| `CONFIG_FIELD_UNKNOWN` | 400 | `field` | config |
| `CONFIG_SCOPE_NOT_ALLOWED` | 400 | `field`, `scope` | config |
| `CONFIG_VALUE_INVALID` | 400 | `field` | config |
| `CONFIG_VALUE_NOT_ALLOWED` | 400 | `field` | config |
| `CONFIG_KEY_PASSPHRASE` | 400 | `field` | config (CLI) |
| `CONFIG_KEY_INVALID` | 400 | `field` | config (CLI) |
| `CONFIG_USE_CLI` | 400 |  | config |
| `CONFIG_NO_TESTER` | 400 |  | config test |
| `LOGIN_UNSUPPORTED` | 400 |  | credential sign-in |
| `CREDENTIAL_DISABLED` | 409 |  | credential sign-in |
| `LOGIN_NOT_OAUTH` | 409 |  | credential sign-in |
| `LOGIN_ALREADY_RUNNING` | 409 |  | credential sign-in |
| `LOGIN_CODE_INVALID` | 400 |  | credential sign-in |
| `LOGIN_NOT_WAITING_FOR_CODE` | 409 |  | credential sign-in |
| `UPLOAD_FAILED` | 400 / 500 |  | uploads |
| `UPLOAD_EMPTY` | 400 |  | uploads |
| `UPLOAD_TOO_LARGE` | 413 | `max_bytes` | uploads |
| `UPLOAD_NOT_AVAILABLE` | 400 |  | uploads |
| `UPLOAD_NAME_EXHAUSTED` | 400 |  | uploads |
| `NO_TESTER` | 200 |  | `POST /api/admin/config/test` result |
| `TESTER_UNAVAILABLE` | 200 |  | `POST /api/admin/config/test` result |
| `TESTER_RAISED` | 200 |  | `POST /api/admin/config/test` result |
| `BAD_RESULT` | 200 |  | `POST /api/admin/config/test` result |
| `SCOPE_ID_INVALID` | 200 |  | `POST /api/admin/config/test` result |
| `SCOPE_UNSUPPORTED` | 200 |  | `POST /api/admin/config/test` result |
| `TOOLBOX_URL_INVALID` | 200 |  | `POST /api/admin/config/test` result |
| `TOOLBOX_UNREACHABLE` | 200 |  | `POST /api/admin/config/test` result |
| `CAPABILITY_UNAVAILABLE` | 200 |  | `POST /api/admin/config/test` result |
| `TOOLBOX_HTTP_ERROR` | 200 |  | `POST /api/admin/config/test` result |
| `TOOLBOX_BAD_BODY` | 200 |  | `POST /api/admin/config/test` result |

**`conversation`**

| Code | Status | Params | Where |
|---|---|---|---|
| `UPLOAD_BUSY` | 503 |  | conversation uploads |
| `CONVERSATION_READ_ONLY` | 409 |  | conversation writes |
| `CURSOR_EXPIRED` | 409 |  | events, history |
| `MESSAGE_NOT_FOUND` | 404 |  | regenerate, unblock |
| `MESSAGE_TOO_LARGE` | 413 |  | submission |
| `TOO_MANY_FILES` | 400 |  | submission |
| `FILES_NOT_ACCEPTED` | 400 |  | submission |
| `TITLE_INVALID` | 400 | `max` | rename |
| `PIN_LIMIT` | 409 | `max` | pin |
| `NO_RUNNING_TURN` | 409 |  | stop |
| `TURN_NOT_RUNNING` | 409 |  | stop |
| `RUN_STILL_STOPPING` | 409 |  | stop |
| `TURN_NOT_STUCK` | 409 |  | unblock |
| `RUN_NOT_FINALIZED` | 409 |  | unblock |

**`miniapps`**

| Code | Status | Params | Where |
|---|---|---|---|
| `NO_PUBLISHED_VERSION` | 409 |  | approve |
| `CURRENT_VERSION_CHANGED` | 409 |  | approve (`current_version` field) |
| `MANIFEST_INVALID` | 409 |  | approve |
| `ACTION_NOT_IN_MANIFEST` | 400 |  | approve |
| `APPROVED_VERSION_GONE` | 409 |  | launch |
| `APP_METADATA_MISSING` | 409 |  | settings |
| `REMOVE_RIGHT_REQUIRED` | 403 |  | members |
| `MEMBER_NOT_FOUND` | 404 |  | members |
| `TITLE_INVALID` | 400 |  | settings |
| `LAUNCH_NOT_CONFIGURED` | 503 |  | launch |
| `ARTIFACT_BUSY` | 503 |  | any write |
| `TOOLBOX_UNAVAILABLE` | 503 |  | any store call |

**`versioned_artifacts`**

| Code | Status | Params | Where |
|---|---|---|---|
| `ARTIFACT_BUSY` | 503 |  | any write |
| `TOOLBOX_UNAVAILABLE` | 503 |  | any store call |
| `ARTIFACT_REFUSED` | 400 / 404 / 409 |  | a store answer with no code |
| `ARTIFACT_NOT_FOUND` | 404 |  | store code |
| `VERSION_NOT_FOUND` | 404 |  | store code |
| `CURRENT_VERSION_CHANGED` | 409 |  | store code (`current_version` field) |
| `VERSION_ALREADY_EXISTS` | 400 / 404 / 409 |  | store code |
| `ARTIFACT_ALREADY_EXISTS` | 400 / 404 / 409 |  | store code |
| `ARTIFACT_ACCESS_DENIED` | 400 / 404 / 409 |  | store code |
| `ARTIFACT_LIMIT_REACHED` | 400 / 404 / 409 |  | store code |
| `ARTIFACT_TOO_LARGE` | 400 / 404 / 409 |  | store code |
| `FILE_TOO_LARGE` | 400 / 404 / 409 |  | store code |
| `TOO_MANY_FILES` | 400 / 404 / 409 |  | store code |
| `INVALID_PATH` | 400 / 404 / 409 |  | store code |
| `PATH_OUTSIDE_ARTIFACT` | 400 / 404 / 409 |  | store code |
| `SYMLINK_NOT_ALLOWED` | 400 / 404 / 409 |  | store code |
| `DRAFT_NOT_FOUND` | 400 / 404 / 409 |  | store code |
| `DRAFT_LOCKED` | 400 / 404 / 409 |  | store code |
| `DESK_MISSING` | 400 / 404 / 409 |  | store code |
| `PUBLISH_FAILED` | 400 / 404 / 409 |  | store code |
| `STORAGE_OPERATION_FAILED` | 400 / 404 / 409 |  | store code |
| `WORKSPACE_UNAVAILABLE` | 400 / 404 / 409 |  | store code |

**`scheduler`**

| Code | Status | Params | Where |
|---|---|---|---|
| `NOT_FOUND` | 404 |  | schedules |
| `FORBIDDEN` | 403 |  | schedules |
| `not_found` | 404 |  | schedules |
| `read_only` | 403 |  | schedules |
| `forbidden` | 403 |  | schedules |
| `vetoed` | 409 |  | schedules |
| `upload_gone` | 400 |  | schedules |
| `upload` | 400 / 413 / 500 | those of `reason` | schedules; the body also has `reason`, one of the `UPLOAD_*` codes above or `INVALID_REQUEST` |
| `busy` | 503 |  | schedules |
| `limit` | 409 |  | schedules |
| `no_occurrence` | 400 |  | schedules |
| `changed` | 409 |  | schedules |
| `owner` | 400 |  | schedules |
| `bad_rule` | 400 |  | schedules |
| `too_frequent` | 400 |  | schedules |
| `too_complex` | 400 |  | schedules |
| `too_far_back` | 400 |  | schedules |
| `bad_timezone` | 400 |  | schedules |
| `bad_start` | 400 |  | schedules |
| `bad_title` | 400 |  | schedules |
| `bad_prompt` | 400 |  | schedules |
| `bad_harness` | 400 |  | schedules |
| `bad_model` | 400 |  | schedules |
| `bad_enabled` | 400 |  | schedules |
| `bad_uploads` | 400 |  | schedules |
| `bad_user` | 400 |  | schedules |
| `missing_field` | 400 |  | schedules |
| `unknown_field` | 400 |  | schedules |
| `view_fixed` | 400 |  | schedules |
| `no_user` | 404 |  | schedules |
| `no_view` | 404 |  | schedules |
| `no_reach` | 409 |  | schedules |
| `overlap` | 409 |  | schedules |
| `too_soon` | 409 |  | schedules |

## Contract deviations

Recorded in [DECISIONS.md](../../DECISIONS.md) (2026-09-25, E2): the checker receives the
capability's scope; visibility is per role; the exchange code is a POST field, not a query
parameter; no manifest seam in E2; `current` resolves through the toolbox.
