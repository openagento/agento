# Scheduler module

The `scheduler` module gives users and agents **editable schedules**: a prompt that runs as a new
task in one agent_view on an RFC 5545 RRULE. The framework holds the mechanism (the `schedule`
table, the RRULE engine and `scheduler:tick`, see [schedules.md](../architecture/schedules.md)).
This module gives the meaning: the `scheduler` schedule source, channel, job type and workflow,
the per-schedule rights, the panel API and four MCP tools.

The module depends on `conversation`, `agent_view` and `web` (`sequence`). Disable it and its
rows stop firing (the tick skips a source that is not registered), its routes answer 404 and its
tools are gone. The rows stay in the database.

## Model

- A **schedule** is one `schedule` row with `source = 'scheduler'`: title, prompt, target
  `agent_view_id`, `rrule`, `starts_at` (naive local time), `timezone`, optional `harness` and
  `model` pins, `enabled`.
- **Owner:** a user (`owner_user_id`) or an agent_view (`owner_agent_view_id`). A user owner runs
  as that user: each job has the requester `user:<id>` with trust `account`. The tick fires a row
  only while its user owner is active and reaches the target view.
- **Target view:** set at create and fixed after. The thread, and so the conversation, is keyed
  by the view. To run the task in another view, delete the schedule and create it again.
- **Conversation:** each schedule has one thread, `schedule:<id>` in the target view, and so one
  conversation. Every run of the schedule is a turn in it. A reply in the conversation runs a
  follow-up in the same thread. When the schedule is deleted, the conversation stays, with
  ` (schedule deleted)` at the end of its title.
- **Shares** (`schedule_member`): the owner (with the share right) gives another user read and
  write. A share never gives share, delete or owner change (D-3).
- **Audit** (`schedule_audit`): one row per create, update, delete, share, unshare, owner change
  and Run now, with the actor (`service:web:user:<id>` from the panel, `job:<id>` from a tool).
- Rows of another source (for example Jira periodic tasks) are shown read-only. Nobody writes them
  here.

## Rights

Rights are per schedule (`src/rights.py`). Every route is `acl: "self"`, because the leaf depends
on the row:

| Group | Rows | Read | Write | Share and delete |
|---|---|---|---|---|
| mine | I own it | `scheduler.own` | `scheduler.own.write` | `scheduler.own.share` |
| shared | a share for me | the share | the share | no |
| agent | an agent_view owns it, target view visible to me | `scheduler.agent` | `scheduler.agent.write` | `scheduler.agent.share` |
| user | another user owns it, target view visible to me | `scheduler.user` | `scheduler.user.write` | `scheduler.user.share` |

- Admin holds every leaf. Only admin changes the owner.
- Create needs `scheduler.own` and a visible target view. The new schedule is mine.
- A row of a read-only source gives read only, to everyone.
- A row the user may not read is 404; a readable row without the right is 403.
- Every write (update, delete, Run now, share, unshare, owner change) checks the rights again
  under the schedule row lock, with fresh reads of the user, the grants and the share rows
  (SEC-7). A share, an owner or a role that changes after the route's check refuses the write.
- A post or a regenerate in the schedule's conversation by its owner or a member is checked
  the same way: the submission locks the schedule row first (the channel's `conversation_lock`),
  then asks the write right again on fresh rows. A share removed or an owner changed after the
  route's check refuses the post with 403, and no message and no job are made. A revoke that
  commits after the post's check waits for the post; that turn was accepted while the right held.

## Limits

| Path | Default | Scope | Meaning |
|---|---|---|---|
| `scheduler/limits/max_per_user` | 10 | default, workspace, agent_view (target view) | schedules one user owns. Admin has no limit. An owner change is not limited. The count runs after a named lock and on a fresh snapshot, so two creates at once cannot both pass it. |
| `scheduler/limits/max_per_agent_view` | 20 | default, workspace, agent_view | schedules one agent_view owns (made by a tool) |
| `scheduler/presets` | 5 presets | default | the panel's recurrence list, `{label: rrule}` |

The core paths `scheduler/min_interval_minutes` and `scheduler/default_timezone` are in
[config/README.md](../config/README.md).

## Run now

`POST /api/scheduler/schedules/<id>/run` fires once through `tick.fire`, with the same lock,
checks and events as the tick. It writes `last_fire_at`, never the anchor. A refusal is 409 with
the code `vetoed`, `no_reach`, `overlap` (a run is still open) or `too_soon` (inside
`min_interval_minutes` of the last fire).

## Tools

All four are off by default (`tools/<name>/is_enabled = 0`), each with its own gate. The caller's
view and job come from the capability, never from an argument. `harness`, `model`, an owner or a
view argument are refused as unknown.

| Tool | Arguments | Does |
|---|---|---|
| `schedule_create` | `title, prompt, rrule, starts_at?, timezone?, enabled?` | Creates a schedule in the caller's view. |
| `schedule_update` | `id`, then any of `title, prompt, rrule, starts_at, timezone, enabled` | Changes a schedule the caller reaches. |
| `schedule_list` | none | Lists the schedules the caller reaches. `editable: false` rows belong to another module. |
| `schedule_delete` | `id` | Soft-deletes a schedule the caller reaches. The tick removes it and retitles its conversation. |

- **Owner (D-11).** When the calling job's requester is an `account` user `user:<n>`, user n owns
  the new schedule. The tool refuses (with no fallback) when that user is not active, does not hold
  `scheduler.own` (admin does), cannot reach the caller view, or is at `max_per_user` (admin is
  not limited). Any other job makes a schedule that the caller view owns, up to
  `max_per_agent_view`.
- **Reach (OP-1).** A tool reaches a row of its own view that its view owns or its job's account
  user owns. A write also needs `source = 'scheduler'`.
- **RRULE.** The tool does a syntax check only (the same refusals as the engine where Node can
  tell: `bad_rule`, `too_frequent`). It leaves `next_fire_at` empty, and the tick computes it
  within a minute. A rule the engine refuses then is disabled, with the reason in `last_error`
  (`schedule_list` shows it).
- **Prompt.** The same rule as the panel: not blank, and at most
  `conversation/limits/max_message_bytes` (default 32768) UTF-8 bytes. Both tools refuse when the
  setting has no value.
- `starts_at` is `YYYY-MM-DDTHH:MM` local in `timezone`; the default is now. `timezone` must be a
  canonical IANA name; the default is `core/scheduler/default_timezone`, else `TZ`, else UTC.

**Known gap (EVT-2):** a create or update from a tool does not dispatch `schedule_save_before` or
`schedule_save_after`: Node has no events. A veto observer does not see tool writes. A delete is
not a gap: the tick dispatches `schedule_delete_after` when it finalizes the row. See ROADMAP.md.

## Panel API

All routes need a session; times are `starts_at` naive local in the row's `timezone`
(`YYYY-MM-DDTHH:MM:SS`), and `next_fire_at`, `last_fire_at` UTC with `Z`. A refusal is
`{"error": "...", "code": "..."}`.

| Method and path | Right | Answer |
|---|---|---|
| `GET /api/scheduler/schedules` | a read leaf or a share (probe) | `{groups: {mine, shared, agent, user}}`, each a list of rows |
| `GET /api/scheduler/options` | as the probe | `{presets: [{label, rrule}], harnesses: [{value, label}], default_timezone, min_interval_minutes, agent_views: [{id, code, label, workspace_id}]}` |
| `POST /api/scheduler/preview` | as the probe | body `{rrule, starts_at?, timezone?}` → `{fires: [{at, local}]}` (next 5) |
| `POST /api/scheduler/schedules` | `scheduler.own` | body `{title, prompt, agent_view_id, rrule, starts_at?, timezone?, harness?, model?, enabled?}` → 201 `{id}` |
| `GET /api/scheduler/schedules/<id>` | read | the row, plus `members: [{user_id, username}]` (null without the share right) |
| `PATCH /api/scheduler/schedules/<id>` | write | any create field, none required → 204. A different `agent_view_id` → 400 `view_fixed` (the same value does nothing) |
| `DELETE /api/scheduler/schedules/<id>` | delete | 204 |
| `POST /api/scheduler/schedules/<id>/run` | write | 201 `{job_id, conversation_id}` or 409 |
| `GET /api/scheduler/schedules/<id>/runs` | read | the newest 10 jobs: `[{id, status, created_at, started_at, finished_at, answer}]`; `answer` is what the last attempt wrote into the conversation (null while it runs, or none). The job's prompt, model and tokens stay behind `GET /api/admin/jobs/<id>` (`admin.jobs`) |
| `GET /api/scheduler/schedules/<id>/member-options` | share | `[{id, username}]` active users except the owner |
| `PUT /api/scheduler/schedules/<id>/members` | share | body `{username}` → 204 |
| `DELETE /api/scheduler/schedules/<id>/members/<user_id>` | share | 204, or 404 when no such share |
| `POST /api/scheduler/schedules/<id>/owner` | admin | body `{user_id}` → 204 |

A row: `id, source, source_ref, title, prompt, agent_view_id, agent_view_code, owner_user_id,
owner_username, owner_agent_view_id, rrule, recurrence_label, starts_at, timezone, harness, model,
enabled, next_fire_at, last_fire_at, last_error, created_at, last_run_status, conversation_id,
can: {read, write, share, delete, change_owner}`.

## Panel

`scheduler/panel/` adds **Schedules** (`/schedules`), shown when the probe `GET /api/scheduler/schedules`
answers 2xx. The list shows the groups that have rows (My schedules also when the user may create
one), polls every 10 s, and has an Enabled switch per row (write right). A schedule opens at
`/schedules/<id>` (`/schedules/new` creates one). The header holds the Enabled switch (on an existing
schedule it saves at once) and the actions menu. The fields are in four sections that open and close;
a closed section shows a one-line summary: **Task** (title, agent view, prompt), **Schedule** (a
preset, or "Custom…" — a dialog that writes the RRULE; start; time zone from the browser's IANA
list), **Agent runtime** (an optional harness and model pin) and **Advanced** (the RRULE text, open by
itself when no preset names the rule). A refusal that names a field shows under that field and opens
its section. Below the form, **Last runs** lists the newest 10 jobs (`GET …/runs`, polled every
10 s): the eye opens the run's answer in a drawer, with a link to the conversation; the details icon
opens the job, as the admin Jobs page shows it, and shows only to a user with `admin.jobs`. The next 5 runs come from `POST …/preview` when a timing field is
committed (blur or pick), not on every key: each check runs the engine in a child process. A save
sends only the changed fields. The agent view is set at create; on an existing schedule its field is
disabled (to move a schedule, delete it and create it again). The open schedule polls every 10 s: a
new row refreshes every field the user did not edit, and the preview when no timing field is edited;
an edit stays as typed (UI-8). The header menu offers, per `can`: Run now (disabled while the form
has unsaved changes, D-21; it opens the run's conversation), Open conversation, Share, Change owner
and Delete. A row from another source (Jira periodic) is read-only.

## Events

The module dispatches `schedule_save_before` (veto; a failed observer also refuses the save),
`schedule_save_after`, `schedule_share_after` and `schedule_owner_change_after` from the panel, and
`schedule_delete_after` (panel delete, and the tick for a tool delete). Run now dispatches
`schedule_run_before` and `schedule_run_after` through `tick.fire`. See
[events.md](../architecture/events.md).
