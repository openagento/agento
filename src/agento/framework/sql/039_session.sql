-- Panel sessions behind the __Host- cookie (E1.5, PRD E2 §4.2). Hashes only.
CREATE TABLE IF NOT EXISTS session (
    id          VARCHAR(64)  NOT NULL PRIMARY KEY,
    token_hash  CHAR(64)     NOT NULL,
    user_id     INT UNSIGNED NOT NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  DATETIME     NOT NULL,
    revoked_at  DATETIME     NULL,
    UNIQUE KEY uk_session_token_hash (token_hash),
    KEY idx_session_user (user_id),
    KEY idx_session_expires (expires_at),
    CONSTRAINT fk_session_user FOREIGN KEY (user_id)
        REFERENCES `user` (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
