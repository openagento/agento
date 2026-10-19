# Per-job harness and model (`job:set-runtime`)

A job can carry its own harness and model instead of taking them from the agent_view
config. This is what lets one agent_view run several jobs on different
`(harness, model)` pairs at the same time — an eval — with no config mutation and no
global lock.

## The contract

`job.agent_type` (the harness id), `job.provider` and `job.model` are **input and
resolved state**, not just output.

| `agent_type` | `model` | Effective harness | Effective model |
|---|---|---|---|
| NULL | NULL | `agent_view/harness` | `agent_view/model` |
| set | NULL | pinned | `agent_view/model` when the view's harness is the pinned one, else the harness default |
| NULL | set | `agent_view/harness` | pinned |
| set | set | pinned | pinned — config is not consulted |

`provider` is never pinned by a caller: it follows the effective harness
(`agent_view/provider` for the view's own harness, otherwise that harness's
`default_provider`).

The model's precedence is `job.model` > the consumer's `--model` (replay) > config.
`agento e2e --harness/--model` pins the job row, so it is the first rule. `agento run
--harness/--model` resolves the same way for one interactive or headless run.

## A harness other than the view's

The workspace build is made for the view's own harness. A job pinned to another harness
gets that harness's files (its config, its toolbox bridge) prepared fresh in its run
dir, on top of the build's shared content. The view harness's own files — its login
among them — are left out of the run.

## Resolve at claim, not at finish

The consumer resolves the three values right after it claims the job (TODO → RUNNING)
and writes them to the row in one statement, before the harness process starts. So a
FAILED, DEAD or retried row shows what the run actually used — which is exactly the row
you need when debugging — and a `config:set` made while a long job runs cannot change it
underneath.

`job.provider` is the **snapshot marker**: a non-NULL provider means a previous attempt
committed a complete runtime, and that runtime is reused verbatim. Inside a committed
snapshot a NULL `model` means the harness default, not "resolve again". A job that died
between the claim and the write has no provider, so it resolves again rather than running
on a half-written row.

A pinned harness that no enabled module registers fails the attempt once, with
`error_class = InvalidJobRuntime` — it is not retried, because waiting cannot fix it.

## Publishing a pinned job

```python
publish(config, AgentType.TODO, "jira", key, harness="codex", model="gpt-x")
```

Neither argument is part of the idempotency key.

## Changing a queued job

```bash
agento job:set-runtime <job_id> --harness codex --model gpt-x
agento job:set-runtime <job_id> --clear     # back to resolving from config
```

Allowed only while the job is `TODO`; an already-claimed job is refused. Every form also
clears the snapshot (`provider`), and a form that replaces or removes the harness drops
the saved `session_id` too — a session belongs to the harness that created it, and the
resume path runs with an empty prompt, so handing a foreign session to another harness
would record a job as successful having done nothing.

A retry and `job:resume` reuse the values the first attempt resolved. An operator who
wants a retry to pick up new config runs `job:set-runtime --clear` first.
