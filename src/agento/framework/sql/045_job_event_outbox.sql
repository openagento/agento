-- PRD E3-E5 §6.4.1 — the framework's transactional outbox.
-- A row is written through the CALLER's cursor, so it commits with the transition it
-- describes: either both are there or neither is.
-- No FK on job_id: jobs are pruned on their own schedule, and §6.4.1 specifies what a
-- relay does when the job is gone.
-- No conversation_id: the framework does not know about conversations. The relay resolves
-- one, which is what keeps this table module-agnostic (PLC-2).
CREATE TABLE IF NOT EXISTS job_event_outbox (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    job_id BIGINT UNSIGNED NOT NULL,
    execution_id VARCHAR(64) NULL,
    kind VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    relayed_at DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_relayed_id (relayed_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
