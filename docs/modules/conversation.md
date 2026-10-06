# Conversation module

Stores chat threads: a `conversation` per thread, its `message` rows, the `execution` each
user turn produced, and the `conversation_event` log a stream replays from. It implements
PRD E3–E5.

- **Module:** `src/agento/modules/conversation/`
- **Depends on:** `agent_view` (`sequence: ["agent_view"]`), so `module:disable agent_view`
  disables this module too.
- **Disabling it** takes chat away and nothing else: the queue, the channels and every other
  module run unchanged, because no framework code imports this module.

See [docs/architecture/conversations.md](../architecture/conversations.md) for the model,
the four identifiers and the event contract. This page is the module's operational detail.

## Tables

| Table | Holds |
| --- | --- |
| `conversation` | One thread: its owner, its agent_view, `active` or `archived`. |
| `message` | One turn. A `user` row carries the job bookkeeping (`job_id`, `execution_id`, `job_state`); a CHECK keeps that off an `assistant` row. |
| `execution` | One attempt at answering a turn. `execution_id` is unique; `(job_id, attempt)` is **not** — the pool-wait path refunds an attempt, so two real attempts can carry one number. |
| `conversation_event` | The append-only log a stream replays. Its `id` is the cursor, and it is global, not per-thread. `UNIQUE (source_kind, source_id)` makes the relay idempotent. |
| `execution_delta` | Per-execution streamed output, bounded by the `stream/max_delta*` limits. |
| `conversation_prune_watermark` | How far retention has pruned each thread. |

`message.job_id` and `execution.job_id` deliberately have **no** foreign key: jobs are pruned
on their own schedule and a message must outlive its job.

## Config

Every numeric field declares `min`/`max` in `system.json`, which is enforced on all three
config levels — see [the config README](../config/README.md#numeric-bounds-min-max).

| Path | Default | Range |
| --- | --- | --- |
| `conversation/claim/defer_backoff_ms` | 1000 | 250 – 30000 |
| `conversation/stream/poll_interval_ms` | 500 | 100 – |
| `conversation/stream/max_per_user` | 4 | 1 – 16 |
| `conversation/stream/heartbeat_seconds` | 15 | 1 – |
| `conversation/stream/max_duration_seconds` | 900 | 1 – |
| `conversation/stream/max_deltas_per_execution` | 2000 | 1 – 20000 |
| `conversation/stream/max_delta_bytes_per_execution` | 1048576 | 1024 – 8388608 |
| `conversation/stream/max_fragment_bytes` | 8192 | 256 – 65536 |
| `conversation/history/page_size` | 100 | 1 – 500 |
| `conversation/sweep/pending_grace_seconds` | 60 | 5 – 3600 |
| `conversation/limits/max_message_bytes` | 32768 | 1024 – 49152 |
| `conversation/retention/event_days` | 90 | 1 – |
| `conversation/retention/outbox_days` | 7 | 1 – |
| `conversation/retention/archived_days` | 365 | 30 – |
| `conversation/retention/idle_days` | 90 | 7 – |

Each retention field reads a different clock: `event_days` and `outbox_days` from the row's
`created_at`, `archived_days` from `conversation.updated_at`, and `idle_days` from the newest
message's `created_at` (a channel thread: its `last_activity_at`). All four comparisons are
strict. `stream/max_fragment_bytes` cuts each string field of one live fragment (its text, a
tool's input or output) before the per-run byte cap counts it.

## REST

| Method | Path | Answers |
| --- | --- | --- |
| POST | `/api/conversation/threads` | `201 {id}` |
| GET | `/api/conversation/threads` | the caller's active threads; `?scope=channels[&channel=<source>][&before=<cursor>]` lists the channel threads instead (admins only, a non-admin gets `[]`), newest activity first, each row with a `cursor` for the next page |
| GET | `/api/conversation/threads/{id}` | one thread, with `live`, `run_details` and its newest 50 `runs` (model, tokens and job id only with `run_details`) |
| GET | `/api/conversation/threads/{id}/timeline` | `?before=<event id>&limit=<n>`: one page of events, oldest first, `{events, has_older, newest_id}`; a `before` at or below the prune watermark is `409 cursor_expired` |
| DELETE | `/api/conversation/threads/{id}` | archives it (§10.1's deletion is an operator path) |
| GET | `/api/conversation/threads/{id}/messages` | its messages |
| POST | `/api/conversation/threads/{id}/messages` | `201` on the first submission, `200` on a repeat, `409 read_only` in a channel thread |

The PRD writes these under `/api/conversations`. A module owns `/api/<its own module name>/`
and nothing else ([panel.md](../architecture/panel.md#module-routes)), so a module named
`conversation` cannot own `/api/conversations`, and the prefix's trailing `/` is why the
collection needs a `threads` segment under it. The rule wins over the spelling
([DECISIONS.md](../../DECISIONS.md)).

A `POST` into an **archived** thread is not refused: it reactivates the thread first and
dispatches `conversation_reactivate_after`, then submits. The body is validated before the
reactivation, so a rejected post never revives a thread it failed to land in.

Every route is session-authenticated, CSRF-checked and rate-limited by `web`'s one dispatch
path — a manifest cannot opt out of any of them.

### One reach gate

`service.load_visible()` is the single place a read resolves a thread. It returns the row
only when the caller owns it (or is `admin`), **and** `can_reach()` covers its
`agent_view_id`, **and** `scope_is_active()` says that scope is still live. Otherwise it
returns `None`, which every route renders as **404, never 403** — a 403 would confirm the
thread exists. Reach is therefore re-evaluated on every request: deactivating the view, or
removing the role's grant, hides the thread from the next one.

### Run details (ACL resource)

The module declares the ACL resource `conversation.run_details` in `di.json`. It covers the trigger
prompt, tool input and output, and a run's model, tokens and job id. `admin` has it built in; a
`user` gets it only by a grant on the thread's workspace or view
(`bin/agento grant:add --role user --operation conversation.run_details --workspace <code>`).
Tool names, statuses and errors stay visible to everyone who reads the thread.

### Title

A thread created with no title takes one from its first message: whitespace collapsed, cut on a
word boundary at 60 characters with `…`. It is set in the submit transaction
(`UPDATE … WHERE title IS NULL`), so a thread created with a title keeps it and an old untitled
thread takes the title of its next message.

`scope_is_active()` is a **new** framework predicate beside `can_reach()`, never folded into
it: E2's admin screens call `can_reach()`/`visible_agent_views()` precisely in order to
administer a deactivated view, and an active check inside them would make such a view
unreactivatable.

A thread whose `agent_view_id` is NULL lost its view to a **delete** (`ON DELETE SET NULL`).
There is no scope left to reach or to deactivate, so it stays readable to its owner — and a
new turn on it answers 404, because there is nothing left to run it.

## Submission: two commits, never one

`POST …/messages` follows §4.1 in three steps: insert the message `pending` and **commit**,
publish the job, then record `job_id` and `published`. One transaction around the insert and
the publish would either announce a job that rolled back or lose a job a message is waiting
on. The cost is a `pending` row after a crash, and the idempotency key is derived from the
message id, so finishing it later re-publishes into the **same** job.

`client_message_id` is unique per thread: a repeat is answered `200` with the same
`message_id`/`job_id`, and only the winning insert writes the `message.created` event — a
replay must not announce one turn twice. `conversation_message_after` follows the same rule:
it is dispatched by the call that publishes the job (the submission, or the sweep finishing a
`pending` message), so a replay announces nothing.

Every text a request body carries — `title`, `client_message_id`, `content` — is checked for
**UTF-8 encodability** at the gate. JSON can carry a lone surrogate (`"\ud800"`) that UTF-8
cannot encode; refused at the gate it is a `400` the caller can act on, while carried past it
the first encode (the column, an SSE frame, a log line) would raise and hand the caller a `500`
for an input that was theirs to fix.

## The run: channel, workflow, and where the prompt stops

The module declares the job type `conversation`, a channel of the same name, and
`ConversationWorkflow`. `job.reference_id` is `"<conversation id>:<message id>"` — written in
the job insert itself, so a claim never reads it half-written — and an unusable one fails
closed with an error that names what it got and what it expected.

The prompt is built from the thread's messages **up to and including that message id**, never
from "every message of the conversation". While turn 1 waits to be claimed the user may
already have queued turn 2: §4.4 defers turn 2's *job*, but nothing would stop turn 1's
*prompt* from swallowing it. The thread would then answer a question the user was never told
had arrived, and turn 2's own job would ask it again.

The channel's own fragments are deliberately short: the task arrived over the panel, the
whole context is the thread, and a channel inventing instructions here would be putting words
in the user's turn.

## The sweep

`conversation:sweep` (cron, every minute) runs both halves of §4.4's backstop:

1. `sweep_pending` finishes every `pending` message older than
   `conversation/sweep/pending_grace_seconds`;
2. `reconcile_terminal` advances a `published` message whose job is already `SUCCESS`,
   `FAILED` or `DEAD`. `TODO`, `RUNNING` and `PAUSED` are left alone: a retry and a stale
   recovery both pass back through `TODO` with the execution finished, and advancing there
   would let the next turn run beside the retry. `PAUSED` is §4.5's case.

Without the second half, a crash between a job's terminal commit and §5.3's finalizer write
strands a message at `published` — and §4.4 reads exactly that column to decide whether the
next turn may run, so the thread would be blocked for ever.

## Hot-reload caveat

`config:set` and `mo:en`/`mo:di` apply live, because the consumer re-runs `bootstrap()` when
idle. Editing this module's **Python** source still needs a container restart
(`docker compose -f docker-compose.dev.yml restart cron`) — `sys.modules` caches it.

## One turn at a time

A thread answers its turns in order. The rule is this module's; the seam is the framework's
`job_claim_before` (see [../architecture/events.md](../architecture/events.md)).
`ConversationOrderingObserver` runs inside the claim transaction: if the thread has an earlier
turn whose `job_state` is still `pending` or `published`, it sets the verdict to `DEFER` and asks
for `conversation/claim/defer_backoff_ms`. The job stays `TODO`, its attempt count untouched, and
the queue offers it again after the backoff.

Three properties follow, and each has a test:

- **The framework names no conversation.** The delay travels in the event; `grep -rn 'conversation/'
  src/agento/framework/` finds nothing. Disable the module and there is no rule (MOD-2).
- **The block is per thread, not per queue.** Another conversation, and every non-conversation job,
  is claimed while one thread waits.
- **A `reference_id` this module cannot read is not an ordering question.** It is claimed and left to
  the workflow, which fails it into the normal retry/dead path — deferring it would hold it forever,
  invisibly.

The refusals are collapsed into a stretch, so a thread blocked for a minute produces one
`job_defer_after` and one `job.deferred` outbox row when the block ends, not sixty.

## The execution seams

The framework owns the job; this module owns the *execution* — the row that says which
attempt produced which output. They meet at the four protocols in
`framework/execution_hooks.py`, declared in this module's `di.json` under `execution_hooks`
and loaded by `bootstrap()`. At most one module may implement each, and `module:validate`
refuses a second before `setup:upgrade` applies a single schema change.

| Seam | Implemented by | With none registered |
| --- | --- | --- |
| `execution_id_provider` | `ConversationExecutionIds` | the execution id is `None` |
| `execution_finalizer` | `ConversationFinalizer` | no finalize write; the transition is today's |
| `resume_session_resolver` | `ConversationResumeSessions` | the shipped attempt-based resume rule |
| `execution_delta_sink` | `ConversationDeltaSink` | deltas are discarded |

A follow-up turn resumes the previous turn's session and is sent the new turn alone. Before
that, the workflow asks the runner's optional `prepare_resume`, which moves the session into
the new run dir; when the session is gone, the turn runs fresh with the whole thread instead
of failing every later turn (see [harness contract](../architecture/harness-contract.md)).

The provider mints a **UUID**, not `{job_id}-{attempt}`: the pool-wait path refunds an
attempt, so two real attempts of one job can carry one number and an id built from the pair
would collide on `execution.uq_execution_id`. It writes the row on the framework's open
connection and does **not** commit — the row commits with the transition that produced it,
and rolls back with it.

The finalizer is called from every transition that ends an *attempt*, not only from the ones
that end the job: a retried attempt closes its execution while its job goes back to `TODO`.
On the recovery paths the framework never held the other process's id, so it passes
`execution_id=None` and the finalizer resolves the row by `(job_id, attempt)`.

`job.failed` is the one row of this flow the **framework** writes, in the same transaction as
the failure: it is a framework job transition, and a module that owned it would make the
module-disabled guarantee silently exclude the event a waiting reader most needs. Its payload
is `job_id`, `attempt`, `execution_id` and a closed four-value `kind`
(`timeout | harness_error | credential_error | internal`) — never exception text, never agent
output.

## The outbox relay

The framework records its own job transitions (`job.queued`, `job.claimed`, `job.deferred`,
`job.failed`) into `job_event_outbox`, in the same transaction as the transition. That table
is framework-owned and names no module (PLC-2). `conversation:relay` — one cron entry, one
process — turns the rows that belong to a thread into `conversation_event` rows.

**The classification order is the contract.** A row is classified by `job.source` first,
never by its reference value:

| The outbox row's job | Outcome |
|---|---|
| gone (jobs are pruned on their own schedule) | terminally relayed, `WARN`, no event |
| `source != 'conversation'` | relayed, no event, whatever its `reference_id` |
| `source = 'conversation'`, resolvable `reference_id` | one event |
| `source = 'conversation'`, unresolvable | terminally relayed, `WARN` |

Nothing is left pending. The resolution reads `message.id`, taken from `reference_id` —
**never** `message.job_id`: §4.1 inserts the job in one transaction and sets `message.job_id`
in the next, so a relay tick between the two would mark `job.queued` relayed with no event
and lose the first event of every thread. The conversation is then read off the message row,
not off the reference's prefix.

Ordering is by outbox `id`, which is why there is exactly one relay and deliberately no
inline post-commit relay beside it: two relays interleave and invert the order that
`conversation_event.id` is supposed to give a thread. Idempotency is
`UNIQUE (source_kind='outbox', source_id)`, so a re-run inserts nothing twice.

With the module disabled the framework still writes its rows — it does not branch on module
state — and they are simply never relayed; `core/outbox/retention_days` is the backstop that
bounds them. `conversation/retention/outbox_days` is the module's own, faster cleanup over
**relayed** rows only: deleting an unrelayed row would lose its event.

### `tool.called`

A tool call is audited by the toolbox, in Node, which knows nothing about conversations.
`conversation:relay` projects those rows in the same tick as the outbox.

`tool_invocation.execution_id` is **not** the run's execution id — the dispatcher mints a
fresh UUID per call and that column is `UNIQUE`. Migration `047` adds `run_execution_id`,
which the dispatcher copies off the auth context, and the projection joins through
`execution` to reach the job. Joining through `toolbox_capability` instead is not an option:
capability rows are purged and the audit outlives them.

The checkpoint is `tool_invocation.conversation_relayed_at`, on the **producer** row. Read
off the event table instead, §10.1's prune would resurrect every `tool.called` it had just
deleted. A call still `pending` is left for the next tick rather than projected with a guess
at its outcome — the dispatcher finalizes every exit.

The payload carries the tool name, the scope, the execution id, the `tool_invocation` id and
the outcome, and **no raw argument or result**: the audit itself only ever held a digest of
the arguments, and an event is one hop closer to a browser than the audit is.

Ordering is bounded on purpose: `tool.called` comes from outside the outbox sequence, so a
tool event may land beside a lifecycle event rather than strictly between two of them. A
reader may assume no more than "after that execution started".

## The finalizer

`execution` and `message` are module tables, and §6.4.1 requires the framework to keep
working with this module disabled, so the framework may not write them. It calls
`ConversationFinalizer` instead — §5.3's `execution_finalizer` seam — on its **own open
connection**, inside the transaction that ends the attempt. Either the transition and these
rows are both there or neither is.

Per call it writes `execution.status` from the outcome **always** (a retried attempt closes
its execution while its job returns to `TODO`; a finalizer bound to the terminal status
alone would leave those rows `running` for ever), and `message.job_state = 'terminal'`
**only** when `job_terminal` — §3.2's rule expressed as an argument rather than as a list of
call sites. It does **not** write `job.failed`: that is a framework job transition, and a
module that owned it would quietly drop it from the module-disabled guarantee.

When the run produced an answer it also writes the assistant `message` row and the
`assistant.message` outbox event. **The event is written only when the insert actually
won.** The two writes have different uniqueness — the message row is pinned by
`(conversation_id, execution_id)`, while every outbox row is a fresh id — so writing the
event unconditionally beside the insert would let a replayed terminal transaction re-use the
one message row and still emit a second event, and the relay, faithful by design, would
deliver the same answer to the thread twice.

The assistant row carries no `job_id` and no `job_state`: the reply is not itself a queued
turn, and §4.4's non-terminal check reads `user` rows only.

The answer is bounded by `conversation/limits/max_message_bytes` and **truncated with a
marker, never rejected** — the run already happened and its answer must land. The cut is on
a codepoint boundary, not a byte offset: slicing the encoded form at an arbitrary offset can
land mid-codepoint and produce bytes that are not UTF-8, which the column, the JSON payload
and the API response would each reject or mangle in turn.

## The blocked state, and the way out

A turn whose job is `PAUSED` with `session_id IS NULL` cannot finish on its own: `resume_job()`
refuses a paused job with no session for ever, and §4.4 then defers every later turn of that
thread. Without a way out, a conversation only a database edit could free would be
indistinguishable, to its owner, from a slow agent.

So the read path says which it is. Every message carries `blocked` and `blocked_reason`:
`paused` for the ordinary case an operator can resume, `paused_unrecoverable` for the trap.

`POST /api/conversation/threads/{id}/messages/{message_id}/unblock` is the way out. Whoever can
**read** the thread may call it — the owner or an admin — because it grants nothing new, it only
lets the thread move again. It sets `message.job_state = 'terminal'` and **does not touch the
job**: the `PAUSED` row stays for an operator to inspect or clean up through the CLI.

Two guards, both required:

1. the job is `PAUSED` with no session, or the message is already terminal. Marking a
   **resumable** turn terminal would let the next turn run beside it when the job resumes —
   exactly what §4.4 exists to prevent.
2. the turn's newest `execution` is no longer `running`. `PAUSED` with no session also holds
   while a stopped process is still alive — a stop request that timed out, or a CLI pause whose
   short wait gave up — and freeing the turn there would start the next one beside that process.
   A non-`running` execution means an acknowledging path has already finalized it (§5.3).

A refusal is `409`, not `404`: the caller can already see the thread, so hiding the reason would
leave them with a thread that is stuck for no stated cause. An unreachable thread is still `404`.

`conversation_unblock_after` is dispatched after the commit, and it is a **reaction seam, not the
audit path** — see DECISIONS.md for why the `admin_audit` row belongs in this transaction rather
than in an observer.

## Retention

`conversation:retention` (`co:ret`, cron `17 3 * * *`) runs four passes in one order that
matters: prune, then retire, then delete, then remove old runs. Pruning first keeps every thread's watermark current,
including ones this pass is about to archive; deleting after them means a thread archived seconds ago
is not also deleted in the same run.

| Pass | Config | What it does |
|---|---|---|
| Prune | `conversation/retention/event_days` (90) | Removes `conversation_event` rows past the window, per conversation, and raises that conversation's prune watermark. Floor: 1 day. |
| Auto-archive | `conversation/retention/idle_days` (90) | Archives a panel thread whose newest message is older than the window (a thread with no messages falls back to its own `created_at`; `conversation.updated_at` does not vote), through `service.archive(..., reason="idle")`. Skips one whose newest **user** turn is not `terminal`. Re-checked per thread under the conversation row lock the posting path takes, so a post that lands mid-pass keeps the thread. A **channel** thread uses `COALESCE(last_activity_at, created_at)` instead (every timeline write moves it) and is skipped while one of its runs is `running`. |
| Delete | `conversation/retention/archived_days` (365) | Deletes an archived thread and everything under it, one transaction each. |
| Old runs | `conversation/retention/event_days` (90) | Deletes every `execution` row that is not `running` and **finished** past the window, with its `execution_delta` rows — oldest first, in batches. One age bound for every run, because an active channel thread lives as long as its issue keeps running (CODE-8). The clock is `finished_at`: a run's events age by `created_at`, so a run that started long ago and finished today keeps its row as long as its events. A run with a finished toolbox call that the relay has not projected yet is kept, because the relay finds the thread through the run's row. |

`conversation/retention/outbox_days` is the relay's own, faster cleanup and belongs to
`conversation:relay`, not to this pass.

The prune is what makes a cursor expirable: see
[docs/architecture/conversations.md](../architecture/conversations.md#retention) for the watermark,
the `cursor_expired` answer on both cursor paths, and why the delete is ordered rather than a
cascade.
