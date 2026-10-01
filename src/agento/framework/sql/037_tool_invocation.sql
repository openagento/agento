-- Tool invocation audit (E1): one row per executeTool call, on both transports.
CREATE TABLE IF NOT EXISTS tool_invocation (
    id                BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    execution_id      CHAR(36)        NOT NULL,
    -- The capability by id, never its value. No FK: expired capabilities are purged,
    -- and the audit record outlives them.
    capability_id     BIGINT UNSIGNED NULL,
    transport         VARCHAR(8)      NOT NULL,
    actor             VARCHAR(16)     NULL,
    subject_id        VARCHAR(128)    NULL,
    on_behalf_of      VARCHAR(128)    NULL,
    tool_name         VARCHAR(128)    NOT NULL,
    -- SHA-256 of the canonical JSON arguments, never the values.
    args_sha256       CHAR(64)        NOT NULL,
    agent_view_id     INT UNSIGNED    NULL,
    workspace_id      INT UNSIGNED    NULL,
    app_artifact_code VARCHAR(64)     NULL,
    app_version_id    BIGINT UNSIGNED NULL,
    app_launch_id     VARCHAR(64)     NULL,
    -- pending until the dispatcher finalizes it; a row left pending is itself a signal.
    outcome           VARCHAR(24)     NOT NULL,
    created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_tool_invocation_execution (execution_id),
    KEY idx_tool_invocation_capability (capability_id),
    KEY idx_tool_invocation_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
