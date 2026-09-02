# Outlook Email Channel

Poll a Microsoft 365 mailbox **per agent_view** for new email — tracked by a durable Graph **delta
cursor**, not `isRead` — and turn each authorized message into a job; the agent reads, replies, and marks
messages processed through MCP tools. Like every Agento
channel, all Microsoft Graph secrets and HTTP live in the **Node.js toolbox** (the zero-trust
boundary); the Python module provides the channel, publisher, polling command, onboarding, and
manifests.

> **Routing model — direct vs routed.** A mailbox owned by exactly **one** agent_view is **direct
> mode**: the mailbox identifies the view, no routing and no binding required (mirrors the Jira
> per-agent_view contract). A mailbox **shared** by two or more agent_views is **routed mode**: the
> inbox is polled **once** (by the lowest-id member) and each message is routed to a view by matching
> the normalized `From` against `outlook_sender` ingress bindings — **regex, case-insensitive
> `fullmatch`, highest `priority` wins**; a tie between **different** views is ambiguous → no job.
> `outlook/allowed_senders` + DMARC remain the inbound **security** gate in both modes. In **direct**
> mode a message is gated by the view's own `allowed_senders` + DMARC. In **routed** mode the gate is
> split around routing (actual execution order): **union-of-per-view-`allowed_senders` pre-filter →
> auto-reply drop → DMARC (global) → mailbox-level activation** (all pre-route) **→ route by `outlook_sender` binding →
> per-view `allowed_senders` refinement against the routed-to view → publish**. So members may hold
> **different** `allowed_senders` (each persona's own safety-net); the SECURITY_BREACH alert stays
> scoped to union-trusted senders. The legacy `ingress:bind email` type is inert for Outlook.
>
> Worked example — shared mailbox `agents@company.com` used by views `sales`, `support`, `vip`:
>
> | Binding | agent_view | priority | A message from… | routes to |
> |---|---|---|---|---|
> | `vip@company\.com` | `vip` | 50 | `vip@company.com` | `vip` (highest priority) |
> | `support@company\.com` | `support` | 20 | `support@company.com` | `support` |
> | `[^@]+@company\.com` | `sales` | 10 | `anna@company.com` | `sales` (catch-all fallback) |
> | | | | `stranger@other.com` | no job (no binding matched) |
>
> Bind with `agento ingress:bind outlook_sender '<regex>' <view> --priority <n>`; inspect with
> `agento ingress:list --type outlook_sender`.

> **Security model in one line:** an inbound email creates a job **only** if its `From` is on the
> `outlook/allowed_senders` allow-list **and** the message passes DMARC. DMARC is **always required**
> (not configurable) — an allow-listed sender whose domain publishes a DMARC policy and fails it is
> treated as a spoof: no job, a `SECURITY_BREACH` log, and an ops alert event. An **automatic reply**
> (RFC 3834 `Auto-Submitted`, e.g. an out-of-office notice) is dropped **before** the DMARC gate — it
> is never a task and never a spoof: no job, no breach alert, just an audit `INFO` line.

## Architecture

| Concern | Where |
|---|---|
| Graph auth (cert **or** client secret) | `toolbox/graph-auth.js` (`@azure/identity`) |
| 5 MCP tools (read / get-attachment / reply / send / mark) | `toolbox/outlook.js` |
| Delta-poll REST endpoint (`POST /api/outlook/delta`, validated cursor resume + paging + DMARC parse) | `toolbox/api.js`, `toolbox/api-handlers.js` |
| Channel prompt fragments + publisher security gate | `src/channel.py` |
| Poll command (`outlook:publish`, cron every minute — loops active agent_views; `--agent-view <code>` for one) | `src/commands/publish.py`, `cron.json` |
| Per-mailbox delta cursor store + table | `src/cursor.py`, `sql/001_outlook_poll_cursor.sql` |
| Typed config (no secrets) | `src/config.py` (`OutlookConfig`) |
| Onboarding | `src/onboarding.py` |

## Configuration

Set via `agento config:set outlook/<key> <value>` (or `CONFIG__OUTLOOK__<KEY>` env). Keys:

| Key | Type | Notes |
|---|---|---|
| `outlook/enabled` | bool | Channel switch — resolved **per agent_view** (set at the view's scope; default `false`). The publisher skips any view whose resolved value is falsy. |
| `outlook/outlook_tenant_id` | string | Azure AD tenant ID (normally at `default` — the global Azure app). |
| `outlook/outlook_client_id` | string | Azure app (client) ID (normally at `default`). |
| `outlook/outlook_client_secret` | **obscure** | Client secret (encrypted at rest). Use **either** this **or** a cert. |
| `outlook/outlook_cert_pem` | **obscure** | Certificate PEM **contents** (cert + private key), encrypted at rest. Takes precedence over the secret when both are set. No file mount. |
| `outlook/outlook_cert_password` | **obscure** | Optional passphrase for an encrypted PEM (leave unset if the PEM is unencrypted). |
| `outlook/outlook_mailbox_user_id` | string | Mailbox UPN to poll — **per agent_view**. A UPN owned by exactly one view is **direct mode** (the mailbox identifies the view); a UPN **shared by ≥2 views** is **routed mode** (the message sender selects the view via `outlook_sender` bindings — see [routing](../architecture/routing.md)). Set at the view's scope for multi-view; `default` works for a single-view deployment (resolved via fallback). |
| `outlook/allowed_senders` | string | **Comma-separated allow-list** of `From` addresses, resolved **per view**. Supports **glob wildcards** (`*@mycompany.com` matches any local part at that domain; `*` never crosses the `@`) and exact addresses. **Empty = block all.** |
| `outlook/poll_top` | int | Delta **page size** per poll, resolved per view, clamped 1..50 (default `10`). The poll pages `@odata.nextLink` to the end, so this caps the per-page size, not the total fetched. |
| `outlook/restrict_read_to_allowed_senders` | bool | **Default `true`.** When on, the agent read tools (`outlook_get_message` / `outlook_get_attachment`) only surface mail that passes the **same gate as the publisher** — sender on `allowed_senders` **and** a DMARC `pass` (the `From` header is forgeable; DMARC is the proof). Verdict undeterminable ⇒ not surfaced (fail-closed); empty `allowed_senders` ⇒ block all reads. Disabling it (`false`) lets the agent read **any** message in the mailbox, including spoofed / non-allow-listed / DMARC-failed mail — a documented **security risk**. (Reads are *additionally* bound to the triggering job's own message — see [Stateless activation & loop safety](#stateless-activation--loop-safety).) |

A DMARC pass is **always required** for allow-listed senders — it is not a config option (see the security gate below).

All per-view keys resolve with the standard 3-tier fallback **agent_view → workspace → default**, so a
view normally inherits the global Azure app credentials and overrides only its `outlook_mailbox_user_id`
(and, if it differs, `enabled`). Two or more views resolving to the **same** mailbox UPN form a
**routed-mode group**: the inbox is polled once (by the lowest-id member) and messages are routed to a
view by sender (see the routing model above). Members of a shared mailbox may hold **different**
`allowed_senders` — each persona's own per-view safety-net, evaluated after routing (the auto-derived
**union** admits any member-trusted sender pre-route; the routed-to view's own list refines
post-route). The **activation** fields (`activation_modes`, `summon_token`,
`direct_requires_sole_recipient`, `mailbox_aliases`, `allow_bot_collaboration`) **must** still be
identical across members — activation runs once with the poll-owner's config, so a divergence there is
logged (`policy_divergence`) and the group is skipped until reconciled. See
[Shared mailbox — per-view inbound policy](#shared-mailbox--per-view-inbound-policy-route-first-authorization).

The **per-agent_view publisher** reads only **non-secret** fields — `enabled`, `poll_top`, `allowed_senders`,
`outlook_mailbox_user_id` (the mailbox UPN, resolved before polling to group views into direct/routed mode),
and the activation/marker keys in [Stateless activation & loop safety](#stateless-activation--loop-safety) —
via per-path config `.get()` (never `get_module()`), so the publisher itself never resolves the obscure Graph
secret. The Graph credentials are consumed **toolbox-side** (that is where all Graph HTTP happens). Note the
precise scope: the Graph secret is still a declared `obscure` field, and — as a **pre-existing**
framework-wide behavior outside this change's scope — the framework `bootstrap()` **transiently
decrypts** DEFAULT/ENV-scope obscure module config on the cron (and consumer, and CLI); the
`OutlookConfig` dataclass drops the secret so it never reaches the job/registry, but the transient
decrypt is real. This is a known limitation tracked by the
[toolbox-only secret-boundary hardening PRD](../security/toolbox-only-secret-boundary.md) — the
regex+priority routing change neither introduces nor worsens it. Loop suppression
introduces **no new secret** — it is address-based and **auto-derived from the agent_views** (no
hand-maintained list), so there is nothing extra to resolve or protect (see
[Stateless activation & loop safety](#stateless-activation--loop-safety)).

### Authentication: certificate or client secret

The toolbox uses `@azure/identity`. Configure **one** credential:

```bash
# Option A — client secret
agento config:set outlook/outlook_tenant_id   <tenant>
agento config:set outlook/outlook_client_id   <client-id>
agento config:set outlook/outlook_mailbox_user_id agenty@mycompany.com
# The secret is an `obscure` field, so it is auto-encrypted (AES-256-CBC). NEVER pass it as the
# positional value (it would leak into `ps aux` and shell history) — omit the value so agento
# prompts/reads stdin, or pipe it in:
agento config:set outlook/outlook_client_secret              # prompts / reads stdin
# or: printf '%s' "$OUTLOOK_CLIENT_SECRET" | agento config:set outlook/outlook_client_secret

# Option B — certificate (PEM contents stored encrypted; NO file mount)
# Easiest: run `agento setup:upgrade`, choose Outlook -> "Certificate (paste PEM contents)",
# paste the full PEM (cert + private key) ending with a line "END", and enter the passphrase if any.
# Or set it manually — outlook_cert_pem is an `obscure` field, so omit the value and let agento
# read it from stdin (NEVER pass a path or the contents as a positional value):
agento config:set outlook/outlook_cert_pem < /path/to/app.pem
agento config:set outlook/outlook_cert_password            # only if the PEM is encrypted
# (tenant_id / client_id / mailbox_user_id as above)
```

**Configure exactly one auth method.** The certificate takes precedence over the client secret in
`graph-auth.js` when both are present. Onboarding clears the other method's keys automatically when you
switch, but if you configure manually you must do it yourself — when switching, `config:remove` the keys
you are no longer using (`outlook/outlook_cert_pem` + `outlook/outlook_cert_password`, or
`outlook/outlook_client_secret`) so stale credentials can't silently win. Config resolves
**ENV → DB → `config.json`**, so if you ever set a `CONFIG__OUTLOOK__OUTLOOK_*` override (e.g.
`CONFIG__OUTLOOK__OUTLOOK_CERT_PEM`), unset that too — `config:remove` only clears the DB row and the
ENV value outranks it.

The Azure app registration needs application permission `Mail.ReadWrite` (and `Mail.Send` to reply/send)
with admin consent.

## The inbound security gate

`OutlookPublisher.publish_mail` enforces, in order:

1. **Normalize** the claimed `From` (strip + lowercase).
2. **Allow-list gate** — if the sender does not match `allowed_senders` (glob: `*@mycompany.com`, exact
   `sklep@mycompanystudio.com`; empty ⇒ block all), the email is skipped (no job) and the mailbox is
   left **untouched** (the publisher never writes `isRead` or moves mail). Ordinary non-routing — only
   the domain is logged, no breach.
3. **DMARC gate (always on)** — for an allow-listed sender the verdict must be `pass`, or **no job is
   published** (fail-closed, no opt-out). An **explicit failure** (`fail`/`quarantine`/`reject`) on a
   domain that publishes a DMARC policy is a probable spoof: a `SECURITY_BREACH` is logged (structured
   `event=security_breach reason=dmarc_not_pass`) **and** a framework `security_breach_after` event is
   dispatched (app_monitor's `SecurityBreachAlertObserver` emails ops when `alerts/*` is configured).
   Any other non-pass (`none`/`bestguesspass`/`temperror`/missing) just means the domain has no usable
   DMARC policy — info log only, no breach, no alert, still no job. The verdict is parsed from the
   **first** `Authentication-Results` header (the one Exchange Online Protection prepends — lower
   headers are untrusted, anti-spoof).
4. **Publish to the agent_view** — in **direct mode** (solo mailbox) the publisher publishes the
   job to that mailbox's view. In **routed mode** (shared mailbox) the sender is matched against
   `outlook_sender` bindings and, after a **per-view allow-list refinement** against the routed-to
   view, the job goes to the resolved target view (job scheduling priority always comes from the
   **target** view, never the ingress binding priority). One job per message either way (idempotency
   `outlook:mail:<id>`), with the `From` stored in `job.requester_email` and `requester_trust = domain`.

The order above is the **direct-mode** gate. In **routed mode** step 2's allow-list is the auto-derived
**union** of the group's per-view lists (a cheap pre-route pre-filter) and a second per-view refinement
runs *after* routing — see the next section.

## Shared mailbox — per-view inbound policy (route-first authorization)

On a mailbox shared by ≥2 views (routed mode), the allow-list half of the gate is **per-view**, decided
around routing. The full routed-mode order (activation runs *inside* admission, i.e. pre-route):

1. **Union pre-filter (pre-route, in-memory).** Reject senders not in the auto-derived **union** of the
   group's per-view `allowed_senders` — cheapest-first, before any routing DB/regex work, and it scopes
   the SECURITY_BREACH alert to union-trusted senders. Computed per poll; no manual union to maintain.
2. **DMARC (global, pre-route).** Unchanged and never per-view — a hard `pass` is always required.
3. **Mailbox-level activation (pre-route).** `activation.decide(...)` runs once with the poll-owner's
   config. Because it is mailbox-level, the five activation fields (`activation_modes`, `summon_token`,
   `direct_requires_sole_recipient`, `mailbox_aliases`, `allow_bot_collaboration`) **must be identical**
   across the group's members — a divergence stalls the group (`policy_divergence`
   `MailboxStalledEvent`) until reconciled. `allowed_senders` is **exempt** from that stall (it is
   per-view).
4. **Route** by `outlook_sender` binding (regex, priority; equal-top to different views ⇒ ambiguous ⇒
   drop). Convention: give the **more specific** binding the **higher** priority (least-privilege).
5. **Per-view `allowed_senders` refinement (post-route, fail-closed).** Re-check the sender against the
   **routed-to** view's own `allowed_senders`, resolved through the standard agent_view → workspace →
   default chain — so a view with no explicit row **inherits the default** (it does *not* silently
   black-hole). A sender admitted via the union but not in the routed-to view's list is dropped
   (cursor advances, no job).
6. **Publish** to the target view.

**Observability.** Post-route drops (unroutable / ambiguous / per-view-allow-list) are surfaced once per
poll as an `inbound_route_drop_after` event **and** an "Outlook routed-poll drop summary" log line
(per-reason counts); the publisher also logs one "Effective outlook policy" line per view at start
(agent_view, mailbox, mode, allowed-senders **count** — never the raw patterns; use
`agento config:resolve outlook --scope agent_view --scope-id <id>` for the resolved values). These drops
are normal traffic, so no alerting observer is wired (unlike the `policy_divergence`/`no_bindings`
misconfiguration alerts).

**Weakest-reachable-persona.** An attacker picks the persona by choosing the `From` sender. Activation is
uniform by construction (the five activation fields are kept identical by the divergence guard), so there
is no weakest *activation* persona; the residual is only that a co-tenant with an over-broad
`allowed_senders` widens who can reach *that* persona — keep each persona's list least-privilege.

**Operator migration (config before code).** (1) Inventory every multi-view UPN and each view's
effective scope-resolved policy; confirm the five activation fields already agree (they were required
identical before, so no change). (2) Ensure unambiguous `outlook_sender` bindings cover each group's
historical sender set; resolve equal-top-priority/different-view ties (they become drops). (3) Seed
per-view `allowed_senders` — start by copying the current default union to every view scope
(`agento config:set outlook/allowed_senders '<union>' --scope agent_view --scope-id <id>`) = no behavior
change, then narrow per persona. (4) Deploy in a low-mail window and watch the first poll — a group
previously stalled on divergent `allowed_senders` un-stalls and runs its never-exercised per-view config
on a backlog burst. (5) Keep the default union as the rollback path until soak proves per-view.

The toolbox read gate `restrict_read_to_allowed_senders` already re-checks the *target* view's
`allowed_senders` at read time, so the publish-time refinement and the read-time gate are consistent by
construction (same view, same matcher).

## Stateless activation & loop safety

After a message clears the inbound security gate above, a second, purely **stateless** decision governs
whether the agent actually acts — computed from the current message alone, with **no thread-state table**
and no persisted state.

### Activation: when the agent may act

An authorized message creates a job **only** when at least one enabled `activation_modes` entry matches:

- **`direct`** — the mailbox (its primary UPN, or any address in `mailbox_aliases`) is the sole recipient
  across `To`+`Cc`. Set `direct_requires_sole_recipient` to `false` to activate on any direct address
  (even when others are also addressed).
- **`mention`** — the `summon_token` (default `@agento`, case-insensitive) appears in the subject or the
  body preview.

Otherwise the publisher **stays silent** — it creates no job but still advances the delta cursor (the
message is evaluated once, not re-clogged). This mirrors the "not for us → leave unread, advance"
pattern; the channel never *holds* on a policy decision.

### Reply is reply-to-all

`outlook_reply` replies to **every** participant Graph will deliver to — `(Reply-To || From) ∪ To ∪ Cc`,
minus the agent's own mailbox — keeping a group thread in one conversation. Every recipient is gated
against `core/email_whitelist`, and **only whitelisted addresses ever receive the reply**. What happens to
a non-whitelisted recipient is governed by **`outlook/reply_policy`**:

- **`remove`** (default) — the blocked recipient is **dropped** from the reply and the message is sent to
  the rest, so one un-whitelisted address in a group thread never blocks the whole conversation. The tool
  result names exactly who was omitted (logged as `dropped=…`), so it is never silent to the agent. The
  reply preserves reply-all buckets (original sender → To, surviving To/Cc → Cc). If **every** recipient is
  blocked, nothing is sent and the tool returns an error (you cannot reply to nobody — use
  `outlook_send_mail`).
- **`block`** — the original strict behavior: if **any** recipient fails the whitelist, the **whole send is
  blocked** and nothing is sent (no draft is created).

Either way the whitelist invariant holds — a blocked address never receives mail; the policy only chooses
between *dropping* it and *blocking the whole send*. `reply_policy` applies to `outlook_reply` only;
`outlook_send_mail` always blocks the whole send (there the agent typed the addresses explicitly). For a
1:1 email reply-to-all is just a reply. Targeted 1:1 mail to a new address still uses `outlook_send_mail`.

### Bot-to-bot loop suppression (fleet-mailbox detection)

To stop two agents from replying to each other forever, the publisher treats an inbound message as
**agent-authored** when its **DMARC-verified `From`** is one of the deployment's other agent mailboxes. That
fleet set is **auto-derived** — never hand-maintained: the toolbox delta handler enumerates the **active
agent_views** and unions each **outlook-enabled** view's resolved `outlook/outlook_mailbox_user_id` (through
the normal `agent_view → workspace → global` fallback), deduped and lowercased, then drops the
currently-polled mailbox so only **other** fleet agents remain (`deriveFleetMailboxes`). Dropping self is
safe: the reply path already excludes the agent's own address, so no self-loop is possible. It then matches
the inbound `From` against that set per message (`isAgentSender`, case-insensitive), and the
publisher **hard-suppresses** agent-authored mail (creates no job) — even when humans are also on the thread
— unless `allow_bot_collaboration` is `true`. Adding or removing an agent_view (or changing its mailbox)
updates the fleet automatically.

This is deliberately **address-based, not a per-message header/HMAC**: Microsoft Graph makes
`internetMessageHeaders` **read-only after a message is created**, so a signed marker header cannot be set on
a `createReplyAll` reply draft — making an outbound-stamped marker unreliable for the main (reply) path. The
`From` is already DMARC-gated before this matters, so it can't be spoofed into a false positive; and a false
positive only ever *suppresses* a reply (the safe direction). An **empty fleet** (a single-view deployment,
or none outlook-enabled) means nothing is treated as agent-authored — loops are still bounded by the
activation rule, since a reply-all in a multi-party thread leaves the agent as one-of-many and it stays
silent. Only agents that are **agent_views in this deployment** are detected; a cross-deployment peer's
mailbox is not part of the fleet. If the derivation ever fails (e.g. a DB blip), it **fails safe to an empty
fleet** — no suppression, loops still bounded by the activation rule.

### Config keys

All resolve with the standard 3-tier fallback **agent_view → workspace → default**.

| Key | Type | Default | Notes |
|---|---|---|---|
| `outlook/activation_modes` | string | `direct,mention` | Comma-set of enabled activation modes. Empty = the agent never activates. |
| `outlook/summon_token` | string | `@agento` | Token that triggers `mention` activation (case-insensitive) in subject/body-preview. |
| `outlook/direct_requires_sole_recipient` | bool | `true` | When on, `direct` fires only if the mailbox/alias is the **sole** recipient across To+Cc. |
| `outlook/mailbox_aliases` | string | `""` | Comma-separated extra addresses that count as this mailbox for `direct` (e.g. an aliased `support@`). |
| `outlook/allow_bot_collaboration` | bool | `false` | When off, inbound mail from a **fleet** agent mailbox is suppressed. The fleet is **auto-derived** from the agent_views (every other outlook-enabled view's mailbox) — no manual list. Turn on to let agents collaborate on a thread. |
| `outlook/reply_policy` | select | `remove` | How `outlook_reply` handles a reply-all recipient not in `core/email_whitelist`. `remove` (default) drops the blocked recipient(s) and sends to the rest; `block` blocks the whole reply if any recipient is not whitelisted. Only whitelisted addresses ever receive mail either way. `outlook_reply` only. |

## Polling: the delta cursor

Poll progress is tracked by a durable Graph **delta cursor**, not `isRead`. This fixes the starvation
bug where rejected/in-flight unread mail clogged a fixed `isRead eq false` window and starved valid mail
behind it.

- **State.** `outlook_poll_cursor` (one row per **normalized mailbox UPN** — the same key the
  publisher groups views by) stores the full `@odata.deltaLink` URL Graph last returned. The publisher
  loads all cursors once per run and passes them to the toolbox.
- **Toolbox (`POST /api/outlook/delta`).** Resumes `mailFolders/Inbox/messages/delta` from the stored
  cursor, **paging `@odata.nextLink` to the end** (no fixed-window truncation), and returns
  `{mailbox, messages, deltaLink, resynced}`. With no cursor it does a full base enumeration.
- **Security.** The cursor arrives in the request body (a route reachable by the zero-trust agent) and is
  fetched with the Graph **app** token (which can read any mailbox), so the toolbox **validates** it
  before use: it must be an `https://graph.microsoft.com` `…/users/{resolvedMailbox}/mailFolders/{folder}/messages/delta`
  URL with no embedded credentials and a `$deltatoken`. A foreign/invalid cursor is **discarded** and a
  full base enumeration runs — no SSRF, no cross-mailbox read.
- **Persist-then-advance.** The publisher gates+publishes, then writes the cursor back **only after** a
  clean pass — and **not** when the batch hit a genuinely transient condition (a publish exception or a
  toolbox/Graph error). A held cursor is re-fetched and re-evaluated next poll. A non-pass DMARC verdict
  (incl. `temperror`) is **not** transient — it is frozen in the immutable receipt-time header, so it
  advances unpublished (holding on it would pin the cursor forever); re-evaluate only via a manual reset.
- **Resync (fail-closed).** A stale/expired cursor (`410`, or a `40x` carrying `syncStateNotFound` /
  `resyncRequired`) triggers a full re-enumeration, not a silent "nothing new". Replays create no
  duplicate jobs — `idempotency_key = outlook:mail:<id>` holds. The toolbox also returns `502` (publisher
  then holds) if it can't reach an `@odata.deltaLink` or can't verify a message's DMARC headers.
- **Manual reset.** `DELETE FROM outlook_poll_cursor WHERE mailbox='<upn>'` forces the next poll to do a
  fail-closed full re-enumeration (idempotent replay).

> **Deployment check:** the cursor validation matches the deltaLink's `/users/{seg}/` against the
> configured UPN. Microsoft Graph normally echoes the UPN you addressed by; if a tenant returns the user
> **object-id** instead, every cursor is rejected → correct results but a full enumeration each poll
> (a bounded-load regression, not a correctness/security bug). Mitigation: also accept the resolved
> object id (a one-time `/users/{upn}?$select=id` lookup).

## Tools are opt-in

All five tools (`outlook_get_message`, `outlook_get_attachment`, `outlook_reply`, `outlook_send_mail`,
`outlook_mark_processed`) ship **disabled**. Enable only what the agent needs:

```bash
agento tool:enable outlook_get_message    --agent-view <code>
agento tool:enable outlook_reply          --agent-view <code>
agento tool:enable outlook_mark_processed --agent-view <code>
```

> **No enumeration tools.** The former `outlook_search_messages` / `outlook_get_new_messages` list tools
> were removed: in a shared mailbox they leaked other people's subjects, senders, and message ids. Message
> discovery is now impossible by construction — see [Reads are bound to the triggering
> message](#reads-are-bound-to-the-triggering-message).

`outlook_send_mail` and `outlook_reply` send external email, so **every** recipient is checked against
`core/email_whitelist` — independent of the inbound allow-list, and only whitelisted addresses ever
receive mail. `outlook_send_mail` blocks the whole send if any recipient fails. `outlook_reply` gates the
full `(Reply-To || From) ∪ To ∪ Cc` set and, per `outlook/reply_policy`, either drops the blocked
recipients (`remove`, default) or blocks the whole send (`block`). See
[Reply is reply-to-all](#reply-is-reply-to-all).

### Reads and thread actions are bound to the triggering message

`outlook_get_message` / `outlook_get_attachment` (reads) **and** `outlook_reply` / `outlook_mark_processed`
(thread actions) accept a message id, but for a headless email-triggered job they operate on **only** the
message that triggered that job. The toolbox resolves the job's own message id once per session from
`job.reference_id` (a scope-checked lookup bound to the job's `agent_view_id` and the `outlook` source,
fail-closed); any other id — even a valid-looking one leaked via a prompt, log, or prior output — returns a
generic error and does nothing (no read, no reply-all into another thread, no marking another mail read).
Combined with the removal of the enumeration tools, the agent cannot reach a conversation it is not part of,
with no ACL and no new tables. (Email is self-quoting, so the triggering message usually carries the prior
thread inline.)

**Operator escape hatch:** an interactive `agento run` has no triggering job (`jobId` is null), so this
binding is not applied — the operator is trusted at a console. The by-construction guarantee applies to
headless, email-triggered jobs.

### Attachments

- `outlook_get_message` returns attachment **metadata** for each attachment (`attachment_id`, `name`,
  `contentType`, `size`, `isInline`, `type` ∈ `file`|`item`|`reference`) — it never fetches the bytes.
- `outlook_get_attachment(message_id, attachment_id)` downloads **one** `file` attachment to the job's
  artifacts dir and returns `{ path, name, contentType, size }`. It **re-applies the same read-gate** as
  the read tools (sender on `allowed_senders` **and** DMARC `pass`) before any download, so it can never
  fetch bytes the read tools could not surface; it rejects non-`file` attachments and anything over 25 MB,
  writes inside the artifacts dir only (a malicious attachment name cannot escape it), never overwrites an
  existing file (a numeric suffix is appended), and ships **opt-in (disabled)**.
- `outlook_reply` and `outlook_send_mail` accept an optional `attachments: string[]` of absolute paths
  inside `/workspace/` (typically files from `outlook_get_attachment`). Caps: **max 10 files, 25 MB each**.
  Files under 3 MB upload via a simple POST; ≥3 MB use a Graph **upload session** (chunked PUT).
  `outlook_send_mail` also accepts `bcc` (whitelist-gated like `to`/`cc`).
- **Shared-mailbox upload-session risk:** upload-session `PUT`s go to `outlook.office.com` with **no auth
  header** — the session URL is the capability (guarded by `isOutlookUploadUrl`: https + `outlook.office.com`,
  no embedded creds). On a **shared mailbox**, treat the in-progress draft / upload session as visible to
  co-owners.

### Preferred sender for new mail

When `outlook_send_mail` is enabled, the agent should use it (not the core SMTP `email_send`) for new
mail: it sends from the agent's real Outlook mailbox (correct `From`, DMARC/SPF aligned) and supports
attachments. The preference is conveyed via the tool descriptions only (a soft LLM hint, never a routing
branch); if `outlook_send_mail` is not enabled, `email_send` remains the automatic fallback (a disabled
tool's description is never shown).

The **read** tools (`outlook_get_message`, `outlook_get_attachment`) are
gated by `outlook/restrict_read_to_allowed_senders` (default **on**): they apply the **same gate as the
publisher** — a message is surfaced only if its sender is on `outlook/allowed_senders` **and** it carries
a DMARC `pass`. The `From` header is forgeable, so the allow-list alone is not enough; without the DMARC
check a spoofed allow-listed sender on a DMARC-failing email would be readable (a prompt-injection
vector). A single-message GET reliably returns `internetMessageHeaders`, so the verdict is parsed
directly (the removed enumeration tools once needed per-message hydration for message collections; with
them gone, no collection is ever surfaced). An undeterminable verdict ⇒
not surfaced (fail-closed); empty `allowed_senders` ⇒ no readable mail. So an enabled read tool can't
expose mail the channel would never have turned into a job (incl. spoofed / DMARC-failed mail sharing the
mailbox). Disabling the flag bypasses **both** checks — a documented security risk.

## End-to-end setup

```bash
agento module:enable outlook
# Global Azure app credentials at the default scope (one app shared across views) — see
# "Authentication" above: tenant_id / client_id / client_secret-or-cert_pem.

# Easiest: `agento setup:upgrade` runs onboarding — it stores the creds at default and, when there is
# more than one active agent_view, prompts you to pick which view owns the mailbox (writing it at that
# view's scope). The manual equivalent, per agent_view (omit --scope/--scope-id for a single-view
# deployment to use the default scope):
agento config:set outlook/outlook_mailbox_user_id agenty@mycompany.com --scope agent_view --scope-id <id>
agento config:set outlook/allowed_senders "sklep@mycompanystudio.com,mklauza@mycompany.com,*@mycompany.com" --scope agent_view --scope-id <id>
agento config:set outlook/enabled 1 --scope agent_view --scope-id <id>

# Enable the channel-critical tools (one per call):
agento tool:enable outlook_get_message    --agent-view <agent_view_code>
agento tool:enable outlook_reply          --agent-view <agent_view_code>
agento tool:enable outlook_mark_processed --agent-view <agent_view_code>

agento setup:upgrade   # installs the outlook:publish crontab (polls every active view's mailbox every minute)
```

(Direct mode — one view per mailbox — needs **no** ingress binding; the mailbox identifies the
agent_view. **Routed mode** — a mailbox shared by ≥2 views — requires `outlook_sender` bindings:
`agento ingress:bind outlook_sender '<regex>' <view> --priority <n>` per view; the message sender
selects the target. The old `ingress:bind email` gate is unrelated and inert. To run the loop for
one view/group manually: `agento outlook:publish --agent-view <code>`.)

### Verifying the acceptance criteria

| Scenario | Expected |
|---|---|
| Mail from `sklep@mycompanystudio.com`, DMARC pass | Job published; `job.requester_email = sklep@mycompanystudio.com` |
| Mail from `test@mycompanystudio.com` (not allow-listed) | No job (skipped); no breach |
| Mail claiming `from: sklep@mycompanystudio.com`, DMARC **not** confirmed | No job; `SECURITY_BREACH` logged |

Confirm a routed job exists:

```sql
SELECT id, source, reference_id, requester_email, requester_trust, agent_view_id, status
FROM job WHERE source='outlook' ORDER BY id DESC LIMIT 5;
```

## Disabling

`agento module:disable outlook` stops the Python side: the module is skipped during bootstrap, so the
`outlook:publish` cron no longer runs and the channel/publisher are not loaded — **no new jobs are
created**. (The toolbox-side tools and the `/api/outlook/delta` route are not yet torn down on
module-disable — a pending follow-up (Task B5); until then they are not triggered by cron but remain
registered/callable, and the tools stay opt-in / disabled by default.) Disabling is safe — no other
module depends on Outlook.
