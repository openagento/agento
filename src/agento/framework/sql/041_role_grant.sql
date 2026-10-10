-- Role grants (E1.5, PRD E2 §5.1): a role may use a toolbox tool or a platform operation.
-- Exactly one of workspace_id / agent_view_id should be set. MySQL 8.0 refuses a CHECK on a
-- column with an FK referential action (ER 3823), so the rule is E2's: writers set exactly one,
-- readers treat a row with both or neither as no grant.
CREATE TABLE IF NOT EXISTS role_grant (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    role          VARCHAR(16)  NOT NULL,
    grant_kind    VARCHAR(16)  NOT NULL,
    name          VARCHAR(128) NOT NULL,
    workspace_id  INT UNSIGNED NULL,
    agent_view_id INT UNSIGNED NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_role_grant_lookup (role, grant_kind, name),
    CONSTRAINT chk_role_grant_role CHECK (role IN ('admin', 'user')),
    CONSTRAINT chk_role_grant_kind CHECK (grant_kind IN ('tool', 'operation')),
    CONSTRAINT fk_role_grant_workspace FOREIGN KEY (workspace_id)
        REFERENCES workspace (id) ON DELETE CASCADE,
    CONSTRAINT fk_role_grant_agent_view FOREIGN KEY (agent_view_id)
        REFERENCES agent_view (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
