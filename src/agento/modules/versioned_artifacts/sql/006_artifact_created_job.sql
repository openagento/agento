-- The job whose run created the artifact (NULL: no job, for example a session run).
ALTER TABLE versioned_artifact ADD COLUMN created_job_id BIGINT UNSIGNED NULL;
