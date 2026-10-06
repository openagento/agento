# Conversations

A conversation is a thread of messages between one panel user and one agent_view. This is the
headline document for the model (PRD E3–E5 §16): what the four identifiers are, how an event
gets from a run to a reader, and which facts the design rests on.

Operational detail — the outbox relay's classification table, the blocked state, the CLI — lives
in [docs/modules/conversation.md](../modules/conversation.md). This page is the shape.

## The model

```
conversation ──< message ──< job ──< execution ──< conversation_event
   (thread)      (a turn)   (queued work)  (one attempt)   (the stream)
```

- **`conversation`** — the thread. It carries its owner, its `agent_view_id` and a status
  (`active` / `archived`). Losing the view to a delete sets `agent_view_id` NULL: the thread stays
  readable history to its owner, and nothing can run in it again.
- **`message`** — one turn, `user` or `assistant`. A user turn is written `pending`, committed,
  then published and advanced to `published` with its job id — two commits, never one, because a
  single transaction around an insert and an external publish either announces a job that rolled
  back or loses a job somebody is waiting on.
- **`job`** — the framework's own queue row. The conversation module owns no scheduler.
- **`execution`** — one attempt at that job. A retried job has several.
- **`conversation_event`** — the append-only projection a reader consumes.

## The four identifiers

| Identifier | Scope | Who mints it | What it is for |
|---|---|---|---|
| `conversation_id` | the thread, for ever | the `conversation` insert | the unit of reach, of archive, of delete |
| `message_id` | one turn | the `message` insert | the prompt's upper bound (`id <= reference_id`), the unblock target |
| `job_id` | one queued turn | `publish_job` | the framework's handle on the work |
| `execution_id` | one attempt | the `execution_id_provider` seam | what a delta, a tool call and a finalized answer are attributed to |

They are not interchangeable and none substitutes for another. Two consequences are worth
stating, because both have already been got wrong once:

- `job.reference_id` is `"<conversation id>:<message id>"`. The prompt ends at that message id —
  not "every message in the thread" — so a turn queued behind this one is never swallowed by it.
- `tool_invocation.execution_id` is the **call's** own id and is UNIQUE. The run's execution id is
  a separate column, `run_execution_id`, copied off the capability. One cannot hold the other.

## The event contract

**Persist before emit.** Everything a reader sees is a row in `conversation_event` first. Nothing
is emitted that was not committed, so a reader that was not listening loses nothing and a reader
that reconnects needs no special path — it replays.

**The cursor is `conversation_event.id`**, a global auto-increment, not a per-thread counter and
not a timestamp. `GET /api/conversation/threads/{id}/events?after=<id>` returns the events after
that id, oldest first, bounded by `conversation/history/page_size` and capped at
`service.MAX_EVENT_PAGE` (500) whatever an operator sets. A client resumes with the last id it
saw; an absent `after` means from the beginning; a non-integer is a 400.

`EventSource` resends the cursor by itself (`Last-Event-ID`, WHATWG HTML §9.2.3), so the browser
client needs no query parameter — but the route accepts one, and a `Last-Event-ID` that is not an
integer degrades to "no cursor" rather than failing.

**Reach is re-checked per request.** Every read route, replay included, resolves through
`service.load_visible`. A cursor is not a grant: deactivate the view or remove the role grant and
the very next replay is 404 — never 403, which would confirm the thread exists.

**One source row yields one event.** `conversation_event` has `UNIQUE (source_kind, source_id)`,
which is what makes the transactional outbox relay idempotent: it may run twice on the same row
and write one event.

### How an event gets there

Two writers, and neither is the agent:

1. **The outbox relay** (`conversation:relay`, every minute) drains `job_event_outbox` — the
   framework-owned, module-agnostic table the consumer writes job transitions into. It classifies
   **by `job.source` first**, never by the reference value: a non-conversation job is relayed with
   no event, a conversation job with a resolvable reference becomes an event, and anything else is
   marked relayed with a WARN rather than retried for ever.
2. **The finalizer** (the `execution_finalizer` seam) writes the assistant message and its
   `assistant.message` outbox row in the same transaction as the execution close.

The toolbox writes `tool_invocation` rows; the relay projects them. No Node code writes
`conversation_event` — an import-boundary test holds that line.

### The stream

`GET /api/conversation/threads/{id}/events/stream` is the live half, and it is a **poll loop**,
not in-process pub/sub: the consumer writes `conversation_event` from the *cron* container and
`web` reads in a different process, so there is no queue the two could share. Polling a table
both can see is the whole mechanism.

Each event goes out as an SSE frame with `id: <conversation_event.id>`, so a reconnect needs no
client code — the browser resends it as `Last-Event-ID`. **No cursor means live**, starting at the
newest event, not a replay of the whole thread: a garbled `Last-Event-ID` is "no cursor", never a
`500` and never `after=0`. A client that **sent** the header gets what the header says, garbled
or not — and a **blank** header is one it sent: `?after=` answers only a client that sent no
header at all, so a stale `?after=0` left in a reconnect URL cannot turn a mangled resume into a
replay of the whole thread. Replay from the beginning is the `?after=` route's job.

No cursor therefore cannot expire, and the question is asked of the **caller's** cursor before one
is invented for it: a thread whose events are all pruned has no newest id to substitute, and the
`0` that would stand in for it is at or below every watermark. The replay route differs on
purpose — there an absent `?after` means *from the beginning*, which is a claim on exactly the
rows a prune took, so it answers `409 cursor_expired`. A `: ping`
comment keeps an idle connection open at `conversation/stream/heartbeat_seconds`.

**The session is re-read on every tick** (§7.4), and the tick re-runs `service.load_visible` — so
a revoked session, a deactivated user, a removed grant and a deactivated agent_view each end the
stream on the next tick, and "the stream closes" and "the read answers 404" are the *same*
decision rather than two rules that drift apart. `stream/max_duration_seconds` closes the server
side; the client's own reconnect resumes from its last id, so a bounded stream costs nothing and
an abandoned one cannot last for ever.

**A user gets `conversation/stream/max_per_user` live streams**, and exceeding it closes the
**oldest**, never refuses the new one. The budget being bounded is threads: `web` is a
`ThreadingHTTPServer` and one open streaming response costs exactly one thread (measured 1:1,
released on close). A refusal would turn a reconnect storm into self-inflicted denial of service
— the client whose stream just dropped is precisely the one asking again, and telling it "no"
leaves it with nothing while its own stale connections hold the budget. A storm therefore
converges on exactly the cap, and the stream just opened is never the one closed (not even when
an operator sets the cap to zero). The registry is per process, like the login throttle.

Every tick commits before it reads. MySQL's REPEATABLE READ would otherwise pin the first tick's
snapshot and the loop would poll for ever without seeing a single new row.

### Live deltas

A third writer, and it is the only one that is optional end to end (§8.2).

```
harness stdout ──> StreamEventMapper ──> bounded queue ──> writer thread ──> ExecutionDeltaSink
   (the harness's own format)   (framework: mechanics only, no SQL)        (the module: every INSERT)
```

Three things must all be true or a run streams nothing extra and its users get §8.1's
behaviour: a module registered an `execution_delta_sink`, the run has an `execution_id`, and the
harness declares a `stream_event_mapper`. Any one missing attaches no callback at all — nothing
is buffered, so nothing is dropped, and no thread starts. The framework **never parses** a
harness's stream format; it asks the harness, exactly as `--pretty` does.

The callback runs on the harness's stdout drain thread and does **no database work** — it maps,
appends to a bounded queue and returns. A query there would stall the run. The queue is lossy on
purpose: when it is full the **oldest** fragment is dropped and a `gap` marker is raised, one
per losing execution per run of drops, never one per dropped record.

The markers are held **beside** the queue, not in it, so a marker costs no queue slot and cannot
itself be dropped by a later overflow. That map has its own ceiling: "one per execution" is bounded
only while the executions are, and a stalled sink plus a flood of short-lived runs is not. Full, the
**oldest** marker is dropped — the run that has waited longest to be told, and the one most likely
already over. One batch is bounded the same way, markers included.

`execution_delta` is the ledger — `(execution_id, seq)` unique, which is what makes a re-delivered
batch harmless — and it is where the two caps are counted:
`conversation/stream/max_deltas_per_execution` and `.../max_delta_bytes_per_execution`. Exceeding
either stops the deltas for that execution and records **one** `truncated` marker. A gap and a
truncation on one execution are two distinct events. **The final assistant message is never
truncated**: a cap on the live stream is not a cap on the answer.

The writer thread is reconciled on every `bootstrap()`, and the key is **the declaration**
(`module name`, declared class path), never the object — the loader builds a new sink on every
pass and the consumer bootstraps every idle poll tick, so keying on identity would restart the
thread every five seconds for ever. The thread adopts the fresh instance instead. Disabling the
module stops the thread and **discards** the queue rather than draining it into a sink whose
tables are no longer ours to write.

The handover lock spans the whole `sink.write(batch)` call, commit included, and a handover takes
that same lock before bumping the generation. The generation is the framework's, in memory; the
transaction is the sink's, and the sink commits it — so a generation checked before `write`
returns says nothing about the moment it commits. Either the batch finished before the handover
boundary or the handover waits for it. A bounded `join()` is never treated as proof the thread
stopped: if a worker outlives it, no replacement starts. The sink's own statement timeout
(`core/sql_timeout_seconds`) is what bounds the handover's wait.

## Every job has a thread (E9)

**Channel threads.** At claim, `ConversationExecutionIds.mint` gives the new `execution` row its
`conversation_id` (`service.link_execution`). A panel job is in the thread of the message it
answers. Any other job (Jira, Outlook, cron) is in its **channel thread**: `user_id` NULL,
`channel` = `job.source`, `external_ref` = `job.reference_id`, and a unique `external_key` =
sha1 of `source|view|reference` (`job:<id>` when there is no reference). The next run on the same
issue, and a follow-up that copies its parent's source and reference, land in the same thread; an
archived one is reactivated. Each run adds `run.started`, and the finalizer adds `run.finished`
and, on success, the answer (`assistant.message`). Channel threads are read-only and an admin's
only (DECISIONS.md D-E9-3).

**The vocabulary.** A harness's `StreamEventMapper` turns its stdout into canonical fragments:
`assistant.text`, `tool.started`, `tool.completed`, `error`, plus the markers `gap` and `truncated`
(see [harness-contract.md](harness-contract.md#adding-live-timeline-events)). The sink stores the
kind as the event kind, with payload `{seq, text, tool_name, data}`. Older rows say
`assistant.delta`; a reader treats them as `assistant.text`.

**Commit-ordered append.** The event id is a global AUTO_INCREMENT, so two writers of one thread
could commit ids out of order and a reader at `id > cursor` would skip one. `service.append_event`
is the one writer: it takes the thread's row lock (an `UPDATE` that also moves
`last_activity_at`) before the insert. A transaction that writes several threads (sink batch,
relay batch) locks them all first, in ascending id. The lock comes before any child write, also
the `message` insert, whose FK check would otherwise take a shared lock first and deadlock.

**One projection.** `service.project_events` builds the client shape for the timeline, the replay
and the stream: it fills `message.created` with the message text, and `run.started` with the
job's prompt (admins only), one query each per page. It drops tool `data.input` / `data.output`
for a non-admin.

**The timeline route.** `GET …/threads/{id}/timeline?before=<id>` pages back from the newest
event; the panel then opens the stream with `?after=<newest_id>`, so the page and the stream leave
no window between them.

## Retention

`conversation:retention` (cron, nightly) runs three passes in one order that matters: prune, then
retire, then delete.

**The prune is by age, per conversation, and it is what moves the watermark.**
`conversation/retention/event_days` removes `conversation_event` rows by `created_at` in **live**
threads, not only in ones being deleted. The predicate is strict `<`, so a row exactly on the cutoff
is kept. `conversation_prune_watermark` is set to the **highest id just removed** — not the highest
surviving one — in the same transaction, and it only ever rises, so a second pass is a no-op.

**That watermark is what makes a stale cursor answerable.** A cursor at or below it is expired, and
both cursor paths ask the same helper: the replay route answers `409 {"error": "cursor_expired"}`,
and the stream yields one `cursor_expired` frame **before any event frame** and ends. Checking only
the route is the worse of the two failures — a browser reconnecting after a prune would be handed
the survivors with the removed rows silently missing and nothing to tell it history is gone. No
cursor can never be expired, and a thread with no watermark row has no expired cursor. This is why
the open-cursor guarantee is withdrawn (ROADMAP).

**Auto-archive retires an idle thread through `service.archive`** — the one archive path, so there
is one event and not two that can drift; `reason` (`idle` vs `manual`) is what tells them apart. A
thread whose newest **user** turn is not `terminal` is skipped: it is waiting for an answer, not
idle, and archiving it would hide a thread the user is still owed a reply on.

Idle is the **newest message's clock**, and nothing else — §10.1's "a conversation whose last
message is older than `idle_days`". `conversation.updated_at` is deliberately not a second
condition beside it: that column moves whenever the row itself is written — an archive, a
reactivation, a title change — so requiring it to be old too means one rename keeps a dead thread
out of retention for ever. A thread with no messages at all has no message clock and falls back to
its own `created_at`, the only activity it has ever had. The bulk select is then re-checked per
thread under the conversation row lock `service.reactivate` takes on the posting path, so an archive
and a post serialize instead of racing.

**The delete is an explicit ordered delete, not a cascade.** One transaction per conversation:
lock the `conversation` row `FOR UPDATE`, re-check under that lock that it is still archived and
still past the window, then delete events, `execution_delta`, `execution`, messages, the watermark
and the conversation — in that order. `execution` and `execution_delta` are found through
`execution.conversation_id`; no cascade reaches them, and dropping the conversation row alone
would leave them for ever. A
reactivation racing the pass either wins (the thread survives whole) or loses (the delete
completes), never half of each, and a crash mid-delete leaves a still-archived conversation the
next run finishes. `service.delete_conversation` (the operator path, no route yet) runs the same function, so it cannot fall back to the cascade either.

## Facts this rests on

Re-verified at `62ef81ca` on `feature/E35-web-sessions`, since the PRD checked its citations
against a different tree.

| Fact | Where | Status |
|---|---|---|
| `web` is a `ThreadingHTTPServer` on `0.0.0.0:8000` | `web/server.py` | confirmed |
| `Route` is a frozen dataclass; `ROUTES` is a list built at import | `web/api.py` | confirmed |
| `LoginThrottle` is a per-process dict (its own note names the DB-backed replacement) | `web/api.py` | confirmed |
| A duplicate publish is an `IntegrityError` → rollback + re-read | `framework/publisher.py` | confirmed |
| Capabilities are minted inside the open claim connection | `framework/consumer.py` | confirmed |
| `dispatch()` instantiates each observer and **swallows** its errors | `framework/event_manager.py` | confirmed |
| Stale-job recovery checks the PID, then falls back to the timestamp | `framework/consumer.py` | confirmed |
| `resume_job()` refuses a PAUSED job with no session, for ever | `framework/job_store.py` | confirmed |

**One open connection costs one thread, released on close.** Measured: 25 concurrent long-lived
responses moved `threading.active_count()` from 2 to 27 and back to 2. §7.3's per-user stream cap
keeps its numbers.

**A `CHECK` constraint may not name a column an FK referential action needs.** MySQL 8.0.45,
reproduced: `ER 3823`, not `ER 3819` (which is a plain violation — a test asserting on the number
must not conflate them). So `role_grant`'s XOR stays writer-enforced.

**Open — owner decision required:** the Caddy 2.11 client-address header spike behind §7.5's
proxy-trust amendment has not been run. §18 marks the amendment open, and running the spike
before the decision would measure a design nobody has approved.

## Related

- [docs/modules/conversation.md](../modules/conversation.md) — the module: relay, blocked state, CLI
- [docs/architecture/events.md](events.md) — the lifecycle events this module dispatches
- [docs/architecture/panel.md](panel.md) — sessions, launch tokens, the per-call capability
