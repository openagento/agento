# Toolbox: log the verified caller, remove the LLM-supplied `user` argument

Date: 2026-11-06. Version: agento-core 1.1.1.

## Problem

22 toolbox tools declare a required `user` argument ("Your (the LLM agent) email address from
SOUL.md — identity credential"). The model types it. Handlers write it into `toolbox_mcp.log` as
`user=<value>`. In production (job 587452, agent_view 1) a resume after a credential failover
made the model pass the OAuth account owner's email, so production DB queries and Jira writes were
logged as made by a human.

The argument is a leftover. It came in the first commit (2026-01-29), before the toolbox had a
verified caller. Since E1 (2026-09-24) every call carries a verified capability
(`toolbox/auth-context.js`) with `actor`, `subject_id`, `agent_view_id`, `workspace_id` and
`job_id`, and `tool_invocation` (`toolbox/dispatcher.js`) already stores them correctly. Only the
text log is wrong.

It has one use beyond the log: `modules/core/toolbox/email.js` sets `replyTo: user`. A
model-supplied address (for example from a prompt injection) controls where replies to agent
mail go, and the `to`/`cc`/`bcc` whitelist does not check it.

## Decisions

1. **Remove `user` now from every shipped tool schema.** No deprecation release. The MCP SDK
   (1.27.1) builds a plain `z.object(shape)`, which strips unknown keys, so a caller that still
   sends `user` (an old prompt, a skill, a saved miniapp page) keeps working.
   Files: `toolbox/adapters/{mysql,mssql,opensearch}.js`,
   `modules/core/toolbox/{browser,schedule,email}.js`, `modules/jira/toolbox/jira.js`.
   Each handler stops writing `user=…` in its log details.

2. **The logger writes the verified identity, once.** `createScopedLogger` (`toolbox/log.js`)
   takes the verified identity from the `authContext` that `buildRegistration`
   (`toolbox/server.js`) already has, for MCP sessions and for invoke-route calls. No handler
   writes identity. Format (field names match the `tool_invocation` columns):

   ```
   [Mieszko (id: 1)] [actor=agent subject=1 job=587452] [jira_add_comment] OK PROJ-12 …
   [Mieszko (id: 1)] [actor=user subject=42 job=-] [jira_add_comment] OK PROJ-12 …
   ```

   - `actor` = `authContext.actor` (`agent` / `user` / `service`); `subject` =
     `authContext.subject_id`; `job` = `authContext.job_id`, or `-` when null.
   - Ids only. No panel user name, no email.
   - With no agent_view (interactive / viewless runs, today the unprefixed `logToolboxMcp`), use
     the same function and leave out only the `[label (id: N)]` part. No tool-call log line has
     an unknown caller.
   - Registration-phase lines (`createPhasedLogger`, to `toolbox_rest.log`) do not change.

3. **No email identity in the log.** No new `agent_view` email field, and no identity
   contributor seam. The Outlook mailbox UPN (`outlook/outlook_mailbox_user_id`) is not used:
   the framework must not read a module's field (PLC, MOD), a routed-mode mailbox is shared by
   two or more views, and many views have none. The mismatch WARN from the issue is not built:
   with no `user` argument, nothing can disagree.

4. **New config field `core/smtp_reply_to`** in `modules/core/system.json`, beside `smtp_from`,
   at every scope (default, workspace, agent_view) like `smtp_from`.
   - Empty (default): `email_send` sends no `Reply-To` header; replies go to `From`.
   - Set to one valid email: it is the `Reply-To`.
   - Set to a value that is not one valid email: `email_send` refuses with an ERROR log line and
     an error result and sends nothing (fails closed, like "SMTP not configured").
   - `email_send` never takes Reply-To from a tool argument.

5. **Docs and skills.** Remove `user` from the examples in `docs/tools/creating-an-adapter.md`
   and `docs/modules/creating-a-module.md`; say that the scoped logger adds the verified caller.
   In `modules/miniapps/skills/miniapp-build/SKILL.md` remove `AGENT_EMAIL`, the `user` argument
   in the `callAction` example and the "Many tools (all Jira tools) require `user`" bullet.
   Document `core/smtp_reply_to` where the email tool / core config is documented. Add a release
   note (CHANGELOG): user modules in `app/code/` should drop their own identity `user`
   argument; the logger now writes the verified caller. No validator bans a parameter named
   `user` (it can be a real business argument).

## Tests

1. A Jira tool, a mysql tool and `email_send` called with an extra
   `user: "human@corp.com"`: the call succeeds, the log line has
   `actor=agent subject=<view id> job=<id>`, and `human@corp.com` appears nowhere in the log.
2. The same kind of call through the invoke route with a `miniapp` (or `user_session`)
   capability logs `actor=user subject=<user id>`.
3. A call with no agent_view logs `[actor=… subject=… job=…]` without the label part.
4. `email_send`: `Reply-To` equals `core/smtp_reply_to` when set; no `Reply-To` when empty; an
   invalid value refuses the send and nothing is sent.
5. Guard test: no shipped tool schema declares a `user` argument described as an identity
   ("identity credential" / "SOUL.md").

## Out of scope

- `tool_invocation` (already correct).
- Third-party tools in `app/code/` (release note only).
- Showing panel user names in the log.

## Acceptance

- No tool handler logs an LLM-supplied argument as identity.
- `toolbox_mcp.log` lines show the verified agent_view, actor, subject and job even when the
  model passes a different `user`.
- `email_send` Reply-To comes only from config.
- `bin/test` passes.
