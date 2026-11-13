# Conversation access seam

A channel conversation (`conversation.channel` is not `panel`) shows one framework thread of a
channel: a Jira issue, a mail conversation, a PR, a schedule. By default such a conversation has
no owner, and only an admin reads it (DECISIONS.md D-E9-3). A channel can change that for its own
conversations through four **optional** members of `Channel`
(`src/agento/framework/channels/base.py`). The `conversation` module calls them; the framework
does not.

The members are not Protocol members. A channel without them keeps loading (CODE-3, PLC-3), and
the caller reads them with `getattr(channel, name, None)`.

## The members

| Member | Called by | Answer |
|---|---|---|
| `conversation_owner(cur, reference_id) -> tuple[int \| None, str] \| None` | `service.link_execution`, when the first run of a thread creates its conversation | `(user_id, title)` of the new conversation. None: no owner, title `<source> <reference_id>`. An existing conversation keeps its owner and title. |
| `conversation_access(conn, user, conversation, mode) -> bool \| None` | the read gate, the write gate, the delete | `mode` is `read`, `write` or `delete`. `conversation` is the conversation row. `user` is None for a system delete (retention). |
| `shared_thread_ids(conn, user, *, before, limit) -> list[int]` | `GET /api/conversation/threads?scope=shared` | the ids of the threads shared with `user`, below `before` (None: from the top), newest first, at most `limit`. |
| `conversation_lock(cur, conversation) -> None` | `service.submit_message`, for a post or a regenerate | nothing. It locks (`FOR UPDATE`) the channel's own rows that grant a write here, in the caller's transaction. The scheduler locks the schedule row. A channel that grants no write from its own rows needs none. |

Every other member only reads, with plain reads (no `FOR UPDATE`), and never commits:
`conversation_owner` runs inside the run's claim transaction, and the `delete` answer runs under
the conversation's row lock. `read` runs once per row of a list page, so keep it cheap. `write`
False is not a veto: an admin and the `conversation.channel_write` grant still pass.

**None is "no opinion"**: the existing gate applies. **An unknown channel** (its module is
disabled or gone) **or a member that raises** grants nothing and refuses a delete (SEC-9). A
failing `conversation_owner`, or one that names a user who is gone, gives an ownerless
conversation, so it never breaks the run's mint.
The panel channel is never asked.

`web` does not run `bootstrap()`. It registers the enabled modules' channels when it loads their
routes (`web/routes_registry.load_module_routes`), so the seam answers there too.

## Where each answer counts

| Gate | Without the seam | With the seam |
|---|---|---|
| Read (`service._reachable`, behind every read route and the stream) | owner or admin, then reach over the view and an active view | also a caller for whom `read` is True. Reach and an active view are still checked, and a conversation with no view is not readable this way. |
| Write (post, regenerate: `service.can_write_channel`) | admin, or the `conversation.channel_write` grant | also a caller for whom `write` is True. The thread still needs an `external_ref` (409 `read_only`) and a view. |
| Rename, pin (`PATCH`) | owner or admin of a panel thread | on a channel thread, its owner only. A reader by share gets 403. |
| Archive (`DELETE` route) | whoever reads the thread | on a channel thread, its owner or an admin. A reader by share gets 403. |
| Delete (`service.delete_conversation`, retention) | always | refused when `delete` is False or the channel is unknown. Retention selects only the conversations of registered channels, and moves a refused row to the back of its queue (`updated_at`), so refused rows do not fill every batch. |
| Unblock, messages, events, timeline, stream | read | read (a reader by share may unblock: it grants nothing new). |

A post and a regenerate are checked again at submission (`service.submit_message` with
`writer=`), in the transaction that inserts the message (SEC-7). The route checked before any
lock, and a share, an owner, a role or a grant can change between the two. The submission:

1. rolls back, to end the route's read snapshot (REPEATABLE READ);
2. calls the channel's `conversation_lock`, before any other read. A failing member refuses (403);
3. reads the user again, then the read gate (`load_visible`) and the write gate
   (`service.write_refusal`, the same gate the route uses) on fresh rows. A refusal rolls back,
   so no message and no job are made;
4. takes the conversation row lock and inserts.

Lock order: the channel's rows, then the poster's `user` row (shared), then the conversation row. The message's author key locks the `user` row at the insert anyway; taking it before the conversation keeps the order a pin uses (`user`, then conversation), so a post and a pin of one owner cannot deadlock.
The channel's writes that take a write right away (the scheduler's unshare, owner change and
delete) lock the same rows first, so they serialize with a post: the post sees the change, or
the change waits for the post. A channel without `conversation_lock` still gets steps 1 and 3,
so the `conversation.channel_write` grant and every channel's `write` answer are fresh. A revoke
that commits after the message does not stop its job: the turn was accepted while the right held.

The owner that grants a rename, a pin or an owner's archive is checked again under the
conversation row lock (`service.update_thread`, `service.archive(owner_id=…)`): an owner change
after the route's check refuses with 403 (SEC-7).

A channel thread is `channel != 'panel'`, never `user_id IS NULL`: a channel conversation may have
an owner. `tests/unit/modules/conversation/test_channel_branch_guard.py` guards this (CLS-1).

## The shared list

`GET /api/conversation/threads?scope=shared[&before=<thread id>]` answers
`{"conversations": [...], "next": <thread id> | null}`. It asks each channel that owns a
conversation for `limit` ids (`conversation/history/page_size`), merges them, keeps the newest
`limit`, loads those conversations (active only) and applies the read gate. `next` comes from the
merged ids, before the gate drops a row, so a page whose rows are all dropped does not end the
list. A page is bounded whatever the number of shares (CODE-8).

## Helpers for a channel module

`conversation.service` has three public helpers a channel module calls in its own transaction:

- `ensure_channel_conversation(cur, *, thread_id, agent_view_id, channel, external_ref, user_id, title, at=None) -> int`
  — create the thread's channel conversation, or reactivate it. An existing one keeps its owner,
  title and reference.
- `retitle(cur, thread_id, title)` — rename the thread's conversation.
- `set_channel_owner(cur, thread_id, user_id)` — move a channel conversation to a new owner and
  drop the old owner's pin. A panel conversation never changes owner.

## The requester of a reply

`message.author_user_id` (migration `009_message_author.sql`) stores who posted. The job a post
starts is published with `user:<author>` and ACCOUNT trust, never with the conversation's owner.
A NULL author (the poster was deleted before publication, a legacy channel message) publishes
with no requester. Before this, a reply in a Jira or Outlook thread carried `user:None`, and an
admin's post in another user's panel conversation carried the owner; both now carry the poster.
