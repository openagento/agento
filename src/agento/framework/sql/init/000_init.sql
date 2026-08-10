-- Agento: consolidated fresh-install schema
-- Equivalent to applying migrations 001 through 028 on a blank database.
-- This file is used ONLY for docker-entrypoint-initdb.d (fresh MySQL init).
-- Incremental upgrades are handled by setup:upgrade using individual migration files.

-- Migration tracking (must exist before anything else)
CREATE TABLE IF NOT EXISTS schema_migration (
    version     VARCHAR(255) NOT NULL PRIMARY KEY,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    module      VARCHAR(255) NOT NULL DEFAULT 'framework'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Workspace & Agent View hierarchy
CREATE TABLE IF NOT EXISTS workspace (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    code        VARCHAR(50)  NOT NULL,
    label       VARCHAR(255) NOT NULL DEFAULT '',
    is_active   TINYINT(1)   NOT NULL DEFAULT 1,
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_workspace_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS agent_view (
    id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    workspace_id  INT UNSIGNED NOT NULL,
    code          VARCHAR(50)  NOT NULL,
    label         VARCHAR(255) NOT NULL DEFAULT '',
    is_active     TINYINT(1)   NOT NULL DEFAULT 1,
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_agent_view_code (code),
    KEY idx_agent_view_workspace (workspace_id),
    CONSTRAINT fk_agent_view_workspace
        FOREIGN KEY (workspace_id) REFERENCES workspace(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Schedule
CREATE TABLE IF NOT EXISTS schedule (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    issue_key     VARCHAR(20)  NOT NULL,
    agent_view_id INT UNSIGNED NULL,
    summary       VARCHAR(500) NOT NULL DEFAULT '',
    agent_type    ENUM('cron', 'todo') NOT NULL,
    cron_expr     VARCHAR(100) NOT NULL DEFAULT '',
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_schedule_agent_view_issue (agent_view_id, issue_key),
    KEY idx_schedule_agent_view (agent_view_id),
    CONSTRAINT fk_schedule_agent_view
        FOREIGN KEY (agent_view_id) REFERENCES agent_view(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Job
CREATE TABLE IF NOT EXISTS job (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    schedule_id     BIGINT UNSIGNED NULL,
    type            ENUM('cron', 'todo', 'followup', 'blank') NOT NULL,
    source          VARCHAR(50) NOT NULL DEFAULT 'jira',
    agent_view_id   INT UNSIGNED NULL,
    priority        TINYINT UNSIGNED NOT NULL DEFAULT 50,
    reference_id    VARCHAR(512) NULL,
    agent_type      VARCHAR(64) NULL,          -- harness id (pre-0.15 name kept)
    provider        VARCHAR(64) NULL,          -- model/API vendor for this run
    model           VARCHAR(50) NULL,
    input_tokens    BIGINT UNSIGNED NULL,
    output_tokens   BIGINT UNSIGNED NULL,
    prompt          MEDIUMTEXT NULL,
    output          MEDIUMTEXT NULL,
    context         TEXT NULL,
    idempotency_key VARCHAR(512) NOT NULL,
    requester_key   VARCHAR(255) NULL,
    requester_email VARCHAR(320) NULL,
    requester_trust VARCHAR(32) NOT NULL DEFAULT 'claimed',
    requester_meta  JSON NULL,
    status          ENUM('TODO', 'RUNNING', 'SUCCESS', 'FAILED', 'DEAD', 'PAUSED') NOT NULL DEFAULT 'TODO',
    attempt         TINYINT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts    TINYINT UNSIGNED NOT NULL DEFAULT 3,
    scheduled_after TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP NULL,
    finished_at     TIMESTAMP NULL,
    result_summary  TEXT NULL,
    error_message   TEXT NULL,
    error_class     VARCHAR(100) NULL,
    pid             INT DEFAULT NULL,
    session_id      VARCHAR(255) DEFAULT NULL,
    toolbox_mcp_calls INT DEFAULT NULL,
    toolbox_mcp_connected BOOLEAN DEFAULT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_job_idempotency (idempotency_key),
    KEY idx_job_dequeue (status, scheduled_after),
    KEY idx_job_schedule (schedule_id),
    KEY idx_job_agent_view (agent_view_id),
    KEY idx_job_priority_created (priority DESC, created_at ASC),
    KEY idx_job_requester_key (requester_key),
    KEY idx_job_requester_email (requester_email),

    CONSTRAINT fk_job_schedule
        FOREIGN KEY (schedule_id) REFERENCES schedule(id) ON DELETE SET NULL,
    CONSTRAINT fk_job_agent_view
        FOREIGN KEY (agent_view_id) REFERENCES agent_view(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Credential registry (credentials stored encrypted inline; see framework/crypto.py).
-- Credentials form a per-scope pool; selection is LRU over healthy (status='ok') rows.
-- `scope` is the open credential scope a harness declares in di.json (e.g. "claude");
-- `agent_type` is the pre-0.15 column, kept NOT NULL and dual-written for one cycle
-- (dropped next release — see ROADMAP.md). Must stay byte-identical to the state
-- produced by 030_credential_scope_and_rename.sql on an upgraded database.
CREATE TABLE IF NOT EXISTS credential (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    agent_type       VARCHAR(64)  NOT NULL,
    scope            VARCHAR(64)  NOT NULL,
    type             VARCHAR(32)  NOT NULL DEFAULT 'oauth',
    label            VARCHAR(100) NOT NULL,
    credentials      MEDIUMTEXT   NULL,
    token_limit      BIGINT UNSIGNED NOT NULL DEFAULT 0,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    status           ENUM('ok','error') NOT NULL DEFAULT 'ok',
    priority         INT          NOT NULL DEFAULT 0,
    error_msg        TEXT         NULL,
    error_source     ENUM('auto','operator') NULL DEFAULT NULL,
    expires_at       DATETIME     NULL,
    throttled_until  DATETIME     NULL,
    lease_owner      VARCHAR(64)  NULL DEFAULT NULL,
    leased_until     DATETIME     NULL DEFAULT NULL,
    used_at          DATETIME(6)  NULL,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    -- Per SCOPE, not global: a label only means anything inside its scope, and the
    -- same label in two scopes is two distinct credentials (see migration 032).
    UNIQUE KEY uq_credential_scope_label (scope, label),
    KEY idx_credential_scope_enabled (scope, enabled),
    KEY idx_credential_pool_select (scope, enabled, status, priority, used_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Usage tracking. credential_id is NULLABLE: a provider that requires no credential
-- still produces usage, attributed by (harness, provider) instead.
CREATE TABLE IF NOT EXISTS usage_log (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    credential_id    BIGINT UNSIGNED NULL,
    harness          VARCHAR(64)  NULL,
    provider         VARCHAR(64)  NULL,
    tokens_used      BIGINT UNSIGNED NOT NULL DEFAULT 0,
    input_tokens     BIGINT UNSIGNED NOT NULL DEFAULT 0,
    output_tokens    BIGINT UNSIGNED NOT NULL DEFAULT 0,
    model            VARCHAR(50)  NULL,
    reference_id     VARCHAR(255) NULL,
    duration_ms      INT UNSIGNED NOT NULL DEFAULT 0,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_usage_credential_time (credential_id, created_at),
    CONSTRAINT fk_usage_credential FOREIGN KEY (credential_id) REFERENCES credential(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Scoped configuration (Magento-style)
CREATE TABLE IF NOT EXISTS core_config_data (
    config_id  INT AUTO_INCREMENT PRIMARY KEY,
    scope      VARCHAR(16)  NOT NULL DEFAULT 'default',
    scope_id   INT          NOT NULL DEFAULT 0,
    path       VARCHAR(255) NOT NULL,
    value      TEXT         NULL,
    encrypted  TINYINT(1)   NOT NULL DEFAULT 0,
    updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_scope_path (scope, scope_id, path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Data patch tracking
CREATE TABLE IF NOT EXISTS data_patch (
    name        VARCHAR(255) NOT NULL,
    module      VARCHAR(255) NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (module, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Ingress identity routing
CREATE TABLE IF NOT EXISTS ingress_identity (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    identity_type   VARCHAR(32)  NOT NULL,
    identity_value  VARCHAR(255) NOT NULL,
    agent_view_id   INT UNSIGNED NOT NULL,
    priority        INT          NOT NULL DEFAULT 0,
    is_active       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_ingress_type_value (identity_type, identity_value),
    KEY idx_ingress_agent_view (agent_view_id),
    KEY idx_ingress_type_active_priority (identity_type, is_active, priority),
    CONSTRAINT fk_ingress_agent_view
        FOREIGN KEY (agent_view_id) REFERENCES agent_view(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Mark all framework migrations as applied so setup:upgrade skips them
INSERT INTO schema_migration (version) VALUES
    ('001_create_tables'),
    ('002_generalize_jobs'),
    ('003_rename_queued_to_todo'),
    ('004_add_followup_type'),
    ('005_agent_manager'),
    ('007_model_and_tracking'),
    ('008_job_prompt_output'),
    ('009_add_blank_type'),
    ('010_core_config_data'),
    ('011_module_migrations'),
    ('012_data_patches_table'),
    ('013_singular_table_names'),
    ('014_workspace_agent_view'),
    ('015_ingress_identity'),
    ('016_job_priority'),
    ('017_job_pid_session_tracking'),
    ('018_job_paused_status'),
    ('019_oauth_token_inline_credentials'),
    ('020_oauth_token_pool'),
    ('021_job_toolbox_mcp_calls'),
    ('022_oauth_token_type_priority'),
    ('023_drop_oauth_token_model'),
    ('024_oauth_token_used_at_precision'),
    ('025_job_toolbox_mcp_connected'),
    ('026_job_requester'),
    ('027_widen_job_reference_keys'),
    ('028_oauth_token_throttled_until'),
    ('029_ingress_identity_priority'),
    -- 030 must be seeded too: this file already creates the post-030 schema, so without
    -- the marker the first setup:upgrade would try to RENAME a nonexistent oauth_token.
    ('030_credential_scope_and_rename'),
    ('031_job_provider'),
    ('032_credential_label_unique_per_scope'),
    ('033_drop_historical_credential_indexes'),
    ('034_credential_error_source_and_refresh_lease');
