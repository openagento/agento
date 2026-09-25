-- Miniapp launches (E1.5, PRD E2 §4.3 and PRD E6 §5). Hashes only.
-- artifact_code and version_id have no FK: versioned artifacts live in the store, not in MySQL.
-- version_id is a VA version id string (v-YYYYMMDD-HHMMSS-xxxx), pinned at launch.
CREATE TABLE IF NOT EXISTS launch (
    id                   VARCHAR(64)  NOT NULL PRIMARY KEY,
    token_hash           CHAR(64)     NOT NULL,
    user_id              INT UNSIGNED NOT NULL,
    artifact_code        VARCHAR(64)  NOT NULL,
    version_id           VARCHAR(64)  NOT NULL,
    manifest_fingerprint CHAR(64)     NOT NULL,
    allowed_actions      JSON         NOT NULL,
    -- The server-side integration config scope the launch pins.
    workspace_id         INT UNSIGNED NOT NULL,
    agent_view_id        INT UNSIGNED NULL,
    created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at           DATETIME     NOT NULL,
    revoked_at           DATETIME     NULL,
    -- The one-time exchange code of PRD E2 §4.3 (query parameter `code`).
    exchange_code_hash   CHAR(64)     NULL,
    exchange_expires_at  DATETIME     NULL,
    exchange_redeemed_at DATETIME     NULL,
    UNIQUE KEY uk_launch_token_hash (token_hash),
    UNIQUE KEY uk_launch_exchange_code_hash (exchange_code_hash),
    KEY idx_launch_user (user_id),
    KEY idx_launch_expires (expires_at),
    CONSTRAINT fk_launch_user FOREIGN KEY (user_id)
        REFERENCES `user` (id) ON DELETE CASCADE,
    CONSTRAINT fk_launch_workspace FOREIGN KEY (workspace_id)
        REFERENCES workspace (id) ON DELETE CASCADE,
    CONSTRAINT fk_launch_agent_view FOREIGN KEY (agent_view_id)
        REFERENCES agent_view (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
