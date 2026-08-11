-- Record WHICH PROVIDER a job ran on, alongside the harness.
--
-- `job.agent_type` stores the harness id, so before this the job row could not say which
-- model vendor served the run. `replay` fell back to the harness's `default_provider`,
-- which silently replays a non-default-provider run on the WRONG provider (a `fake_cloud`
-- run replays as `fake_local`), and the job record could not answer "what did this cost,
-- and where" without joining usage_log.
--
-- Nullable: rows written before this migration genuinely do not know their provider, and
-- guessing one would be worse than admitting it. Replay treats NULL as "fall back to the
-- harness default" exactly as before.
ALTER TABLE job ADD COLUMN provider VARCHAR(64) NULL AFTER agent_type;

-- Widen `agent_type` to match the harness-id contract. `HarnessDescriptor` allows ids up
-- to ID_MAX_LENGTH = 64 (chosen to match `credential.scope VARCHAR(64)`, widened the same
-- way in migration 030), but this column was still the pre-0.15 VARCHAR(20) sized for
-- 'claude'/'codex'. A valid 21-64 character third-party harness id would fail job
-- finalization on the SUCCESS UPDATE.
ALTER TABLE job MODIFY agent_type VARCHAR(64) NULL;
