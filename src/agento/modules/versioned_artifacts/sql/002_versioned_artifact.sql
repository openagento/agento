CREATE TABLE IF NOT EXISTS versioned_artifact (
    artifact_code VARCHAR(64) NOT NULL,
    title VARCHAR(255) NULL,
    owner VARCHAR(255) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artifact_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
