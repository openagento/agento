# app_monitor

Application health monitoring. Currently two responsibilities:

1. **MCP-health telemetry (`job_finalize_before`)** — when the agent reports
   `rc=0`, `McpHealthTelemetryObserver` records two **independent, nullable**
   per-attempt signals on the `job` row. It is **pure telemetry**: it never sets
   a verdict and never disrupts job flow — an rc=0 job stays a `SUCCESS`.

   | Column | Meaning |
   |---|---|
   | `job.toolbox_mcp_calls` (`INT NULL`) | Count of `mcp__toolbox__*` tool-uses observed in the on-disk session transcript (parsed via the provider's registered `TranscriptReader`). `0` = parsed cleanly, none found. `NULL` = unknown: no reader for the provider, missing/unreadable transcript, or parser drift. |
   | `job.toolbox_mcp_connected` (`BOOLEAN NULL`) | What the CLI self-reported for the `toolbox` MCP server in its session-init line (from `RunResult.mcp_init`), mapped tri-state. See semantics below. |

   `toolbox_mcp_connected` semantics — the status is the CLI's **own vocabulary**
   and an open string on the wire, so it is mapped **tri-state**. `FALSE` and
   `NULL` mean different things:
   - **`TRUE`** = an init report exists **and** `toolbox` is listed
     `status="connected"`.
   - **`FALSE`** = the CLI reported something terminal for this session —
     `failed` / `needs-auth` / `needs-approval` / `disabled`
     (`MCP_STATUS_NOT_CONNECTED`) — **or** `toolbox` is absent from the reported
     servers entirely (including the empty-list case — a valid report saying "I
     started, no MCP servers visible"). "Init present, toolbox not visible" is
     `FALSE`, **not** `NULL`.
   - **`NULL`** = "we don't know", for either of two reasons: the provider exposed
     **no init report at all** (e.g. Codex today — see below — or a Claude stream
     with no `system/init` line), **or** the reported status is merely
     indeterminate.

   **`pending` is normal, not a failure.** Claude connects MCP servers
   non-blocking by default: it seeds every server `pending`, prints `system/init`,
   and connects afterwards — and in `-p`/stream-json mode it never re-reports. So
   `pending` is a snapshot from *before* the handshake, it resolves to `NULL`
   (silently — it would otherwise warn on every job), and a run can legitimately
   show `pending` alongside dozens of successful `mcp__toolbox__*` calls. What
   makes `TRUE` reachable at all is `"alwaysLoad": true` on the toolbox entry in
   the generated `.mcp.json` (see [docs/architecture/containers.md](../../../../docs/architecture/containers.md)),
   which makes the CLI await that one handshake before emitting init.

   A status word in **none** of the three sets is treated as `NULL` too — a
   renamed status must never read as an outage — but logs a `WARNING` naming it,
   so extending `MCP_STATUS_*` in `src/constants.py` is an obvious follow-up.

   Both signals are written on **every** attempt in a single `UPDATE` — including
   to `NULL` — because `job` rows are reused across retries; rewriting both
   columns each attempt prevents a prior attempt's values from going stale.

   **Optional alert** — when `send_alert_on_mcp_issues` is on **and** SMTP is
   configured, one email is sent per attempt if **at least one explicit-bad
   signal** is present: `toolbox_mcp_calls == 0` **OR** `toolbox_mcp_connected IS
   FALSE`. `NULL` ("unknown") never triggers an alert — which is why the tri-state
   mapping above matters: a `pending` init on a healthy job is `NULL`, so it stays
   silent, while `calls == 0` still catches a job that never reached the toolbox.
   A combined hit sends a single email naming both conditions, and the subject/body
   carry the raw status word (`toolbox not connected (failed)`,
   `Toolbox status: absent`) so ops can tell *why* without opening a transcript.

   The transcript parser lives in the agent's module (claude/codex/…); this
   observer resolves one via `get_transcript_reader(provider)`, so the
   framework — and this module — stay agent-agnostic.

   **Codex init signal — empirical finding.** `codex exec --json` (verified
   through 0.128.0 against a real production session, fixture
   `tests/fixtures/codex/real_success_with_mcp.ndjson`) emits **no** session-level
   MCP-server init self-report. The only event types observed are
   `thread.started`, `turn.{started,completed,failed}`, `item.{started,completed}`
   (with `item.type` ∈ {`agent_message`, `command_execution`, `mcp_tool_call`}),
   and `error`. MCP only ever surfaces as per-call `mcp_tool_call` items — which
   report a tool was *invoked*, not whether the server *connected* at startup.
   Consequently `_populate_mcp_init` leaves `RunResult.mcp_init = None` for Codex,
   and `toolbox_mcp_connected` stays `NULL` for codex jobs. Claude *does* emit a
   `system/init` line listing `mcp_servers`, so its column is populated. If a
   future Codex version ships a real init event, wire it into `_populate_mcp_init`
   and add a `tests/fixtures/codex/with_mcp_init.ndjson` fixture.

2. **DEAD-letter alerting (`job_dead_after`)** — `AlertEmailObserver` sends a
   plain-text alert via SMTP when `alerts/email_to` and `alerts/smtp_host`
   are configured. Silent no-op if either is empty; SMTP failures are logged
   but never propagated. (This fires on DEADs caused by *other* framework
   errors — `app_monitor` itself no longer dead-letters anything.)

## Disable

```bash
agento module:disable app_monitor
```

Disabling stops telemetry (columns stay `NULL`) and all email alerts. Job flow
is unaffected — `rc=0` → `SUCCESS` either way.

## Tune

```bash
agento config:set app_monitor/send_alert_on_mcp_issues true
agento config:set app_monitor/alerts/email_to ops@example.com
agento config:set app_monitor/alerts/smtp_host smtp.example.com
```

Env overrides also work, e.g.
`CONFIG__APP_MONITOR__SEND_ALERT_ON_MCP_ISSUES=true`.

A daily MCP-health snapshot:

```sql
SELECT COUNT(*), toolbox_mcp_connected, AVG(toolbox_mcp_calls)
FROM job WHERE created_at > NOW() - INTERVAL 1 DAY
GROUP BY toolbox_mcp_connected;
```
