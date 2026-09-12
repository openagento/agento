CREATE TABLE IF NOT EXISTS versioned_artifact_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    artifact_code VARCHAR(64) NOT NULL,
    operation VARCHAR(48) NOT NULL,
    draft_id VARCHAR(64) NULL,
    version_id VARCHAR(64) NULL,
    previous_version VARCHAR(64) NULL,
    revision VARCHAR(40) NULL,
    job_id BIGINT UNSIGNED NULL,
    agent_view_id INT UNSIGNED NULL,
    actor VARCHAR(255) NULL,
    result VARCHAR(16) NOT NULL,
    error_code VARCHAR(32) NULL,
    description VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_artifact_created (artifact_code, created_at),
    KEY idx_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
