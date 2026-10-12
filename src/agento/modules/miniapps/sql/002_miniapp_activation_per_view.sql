-- One approval per (artifact, agent_view) that pins a version and its content (plan D-MINIAPP-2).
-- The per-version rows have no view, so they go: each live app needs one Approve after the upgrade.
-- No FK to versioned_artifact: that row is best-effort; delete cleanup is explicit.
DROP TABLE IF EXISTS miniapp_activation;
CREATE TABLE miniapp_activation (
    artifact_code VARCHAR(64) NOT NULL,
    agent_view_id INT UNSIGNED NOT NULL,
    approved_version_id VARCHAR(64) NOT NULL,
    approved_content_id CHAR(40) NOT NULL,
    allowed_actions JSON NOT NULL,
    activated_by VARCHAR(255) NOT NULL,
    activated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artifact_code, agent_view_id),
    CONSTRAINT fk_ma_view FOREIGN KEY (agent_view_id) REFERENCES agent_view (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
