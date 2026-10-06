-- 049: an index for the run's execution id on the tool audit (047 added the column only).
-- Two readers join on it per row: the conversation relay's tool projection, and the
-- retention guard that keeps a run while one of its calls is not projected yet.
ALTER TABLE tool_invocation
    ADD KEY idx_tool_invocation_run_execution (run_execution_id);
