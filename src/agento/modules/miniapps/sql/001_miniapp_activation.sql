-- An activated miniapp version (PRD E6 §3, §4): the manifest it was activated with, by
-- fingerprint, and the actions the operator allowed. Deactivation deletes the row.
CREATE TABLE IF NOT EXISTS miniapp_activation (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    artifact_code VARCHAR(64) NOT NULL,
    version_id VARCHAR(64) NOT NULL,
    manifest_fingerprint CHAR(64) NOT NULL,
    allowed_actions JSON NOT NULL,
    activated_by VARCHAR(255) NOT NULL,
    activated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_miniapp_activation (artifact_code, version_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
