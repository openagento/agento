CREATE TABLE IF NOT EXISTS toolbox_capability (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    token_hash    CHAR(64)        NOT NULL,
    kind          VARCHAR(32)     NOT NULL,
    -- NULL only for a viewless internal_rest capability (a default-scope config test).
    agent_view_id INT UNSIGNED    NULL,
    job_id        BIGINT UNSIGNED NULL,
    expires_at    DATETIME        NOT NULL,
    revoked_at    DATETIME        NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_toolbox_capability_hash (token_hash),
    KEY idx_toolbox_capability_job (job_id),
    KEY idx_toolbox_capability_expires (expires_at),
    -- A deleted agent_view takes its live capabilities with it, so a token can never
    -- outlive the scope it names and land on the strict resolver's error path instead.
    CONSTRAINT fk_toolbox_capability_view FOREIGN KEY (agent_view_id)
        REFERENCES agent_view (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
