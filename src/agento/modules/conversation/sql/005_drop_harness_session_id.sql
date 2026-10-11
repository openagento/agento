-- One session store: `job.session_id`. Nothing ever wrote `execution.harness_session_id`,
-- so the column is dropped, not migrated. 001 is not edited (MOD-3).
ALTER TABLE execution DROP COLUMN harness_session_id;
