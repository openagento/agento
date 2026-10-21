-- A job that an agent_run tool call created records its parent job and its depth.
-- Existing rows: parent_job_id NULL, depth 0 = a root job.
ALTER TABLE job
  ADD COLUMN parent_job_id BIGINT UNSIGNED NULL AFTER schedule_id,
  ADD COLUMN depth TINYINT UNSIGNED NOT NULL DEFAULT 0 AFTER parent_job_id,
  ADD KEY idx_job_parent (parent_job_id),
  ADD CONSTRAINT fk_job_parent FOREIGN KEY (parent_job_id) REFERENCES job(id) ON DELETE SET NULL;
