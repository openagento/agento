-- 047: the run's execution id on the tool audit, and the relay's own checkpoint
-- (PRD E3-E5 §6.4.2).
--
-- `tool_invocation.execution_id` is NOT the run's execution id: the dispatcher mints a
-- fresh UUID per tool call and that column is UNIQUE. The run's id arrives on the auth
-- context, from the capability row, and gets its own column here.
--
-- Joining through `toolbox_capability` instead is not an option: capability rows are purged
-- and the audit outlives them.
--
-- `conversation_relayed_at` is the projection's checkpoint, on the producer row rather than
-- on the event: §10.1 prunes events, and a checkpoint kept only there would let a pruned
-- `tool.called` be projected again on the next tick.
ALTER TABLE tool_invocation
    ADD COLUMN run_execution_id VARCHAR(64) NULL AFTER execution_id,
    ADD COLUMN conversation_relayed_at DATETIME NULL,
    ADD KEY idx_tool_invocation_conversation_relay (conversation_relayed_at, id);
