CREATE TABLE IF NOT EXISTS versioned_folder_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    folder_code VARCHAR(64) NOT NULL,
    operation VARCHAR(48) NOT NULL,
    draft_id VARCHAR(64) NULL,
    version_id VARCHAR(64) NULL,
    previous_version VARCHAR(64) NULL,
    revision VARCHAR(40) NULL,
    job_id BIGINT UNSIGNED NULL,
    agent_view_id INT NULL,
    actor VARCHAR(255) NULL,
    result VARCHAR(16) NOT NULL,
    error_code VARCHAR(32) NULL,
    description VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_folder_created (folder_code, created_at),
    KEY idx_job (job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
