-- 048: provider limits on a credential, and the panel re-login request.
--
-- `limits` is what `credential:limits` last read from the provider (`{"windows": [...],
-- "balance_usd": ...}`), NULL when there is nothing to show. A telemetry column.
ALTER TABLE credential
    ADD COLUMN limits    JSON     NULL,
    ADD COLUMN limits_at DATETIME NULL;

-- One panel re-login. `web` inserts it (`pending`); the cron worker `credential:web-login`
-- claims it and drives the vendor CLI. The pasted code is never stored in plain text:
-- `code_key` is the PEM public half of a key pair that only the worker's memory holds, and
-- `code_box` is the code sealed with it (RSA-OAEP-SHA256). Both are cleared on every
-- terminal write.
CREATE TABLE IF NOT EXISTS credential_login (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    credential_id BIGINT UNSIGNED NOT NULL,
    status        ENUM('pending','starting','waiting','verifying','done','failed','cancelled') NOT NULL,
    verify_url    VARCHAR(2048)   NULL,
    user_code     VARCHAR(64)     NULL,
    needs_code    BOOLEAN         NOT NULL DEFAULT FALSE,
    code_key      TEXT            NULL,
    code_box      VARBINARY(1024) NULL,
    error_code    VARCHAR(32)     NULL,
    created_by    INT UNSIGNED    NULL,
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    heartbeat_at  DATETIME        NULL,
    expires_at    DATETIME        NOT NULL,
    KEY idx_credential_login_credential (credential_id, status),
    CONSTRAINT fk_credential_login_credential FOREIGN KEY (credential_id)
        REFERENCES credential (id) ON DELETE CASCADE,
    CONSTRAINT fk_credential_login_user FOREIGN KEY (created_by)
        REFERENCES `user` (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
