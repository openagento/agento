-- Two lookups this module performs have no covering index (CODE-8).
--
-- `conversation_event (execution_id, kind)` — the finalizer deletes one execution's live
-- fragments when the run ends; without it the delete scans the thread's whole retained
-- history while holding the thread's row lock.
--
-- `message (job_id)` — the finalizer marks a channel reply terminal by its job id, and
-- `reconcile_terminal` joins `job` on the same column.
ALTER TABLE conversation_event ADD KEY idx_execution_kind (execution_id, kind);
ALTER TABLE message ADD KEY idx_job_id (job_id);
