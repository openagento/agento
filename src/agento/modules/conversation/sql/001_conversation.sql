-- PRD E3-E5 §3.2 — conversations, messages, executions and the event log.
-- Every FK states its ON DELETE: what survives a deleted parent is a schema decision,
-- not something each caller should have to remember.

CREATE TABLE IF NOT EXISTS conversation (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    -- SET NULL: a deleted view must not make the thread undeletable or unreadable
    -- (the repo's convention for this FK — sql/014_workspace_agent_view.sql).
    agent_view_id INT UNSIGNED NULL,
    -- RESTRICT: a user who owns threads is deactivated, not deleted (E2's model).
    user_id INT UNSIGNED NOT NULL,
    title VARCHAR(255) NULL,
    status ENUM('active', 'archived') NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_user_status_updated (user_id, status, updated_at),
    CONSTRAINT fk_conversation_agent_view FOREIGN KEY (agent_view_id)
        REFERENCES agent_view (id) ON DELETE SET NULL,
    CONSTRAINT fk_conversation_user FOREIGN KEY (user_id)
        REFERENCES `user` (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS message (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    -- CASCADE is belt-and-braces only: §10.1's delete is explicit and ordered.
    conversation_id BIGINT UNSIGNED NOT NULL,
    role ENUM('user', 'assistant') NOT NULL,
    content MEDIUMTEXT NOT NULL,
    client_message_id VARCHAR(128) NULL,
    -- No FK on purpose: jobs are pruned on their own schedule and a message outlives its job.
    job_id BIGINT UNSIGNED NULL,
    execution_id VARCHAR(64) NULL,
    job_state ENUM('pending', 'published', 'terminal') NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_conversation_client_message (conversation_id, client_message_id),
    UNIQUE KEY uq_conversation_execution (conversation_id, execution_id),
    KEY idx_conversation_id (conversation_id, id),
    KEY idx_job_state_created (job_state, created_at),
    -- Job bookkeeping belongs to the user turn that started the job.
    CONSTRAINT chk_message_job_state CHECK (job_state IS NULL OR role = 'user'),
    CONSTRAINT fk_message_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversation (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS execution (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    execution_id VARCHAR(64) NOT NULL,
    job_id BIGINT UNSIGNED NOT NULL,          -- no FK, same reason as message.job_id
    attempt INT UNSIGNED NOT NULL,
    harness_session_id VARCHAR(255) NULL,
    harness VARCHAR(64) NULL,
    provider VARCHAR(64) NULL,
    model VARCHAR(64) NULL,
    status ENUM('running', 'succeeded', 'failed', 'abandoned') NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_execution_id (execution_id),
    -- NOT unique: the pool-wait path refunds the attempt and the next claim increments it
    -- again, so two real attempts can carry one number.
    KEY idx_job_attempt (job_id, attempt)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS conversation_event (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,   -- the stream cursor: global, not per-thread
    conversation_id BIGINT UNSIGNED NOT NULL,
    execution_id VARCHAR(64) NULL,
    kind VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    source_kind VARCHAR(16) NOT NULL,
    source_id BIGINT UNSIGNED NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    -- One source row yields one event: this is what makes the outbox relay idempotent.
    UNIQUE KEY uq_source (source_kind, source_id),
    KEY idx_conversation_id (conversation_id, id),
    KEY idx_created_at (created_at),
    CONSTRAINT fk_conversation_event_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversation (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS execution_delta (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    execution_id VARCHAR(64) NOT NULL,        -- the string id, not execution.id — no FK
    seq INT UNSIGNED NOT NULL,
    kind ENUM('delta', 'truncated', 'gap') NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_execution_seq (execution_id, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS conversation_prune_watermark (
    conversation_id BIGINT UNSIGNED NOT NULL,
    last_pruned_event_id BIGINT UNSIGNED NOT NULL,
    pruned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (conversation_id),
    CONSTRAINT fk_prune_watermark_conversation FOREIGN KEY (conversation_id)
        REFERENCES conversation (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
