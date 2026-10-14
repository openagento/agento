-- PRD E9 — one timeline for every run: channel threads, the run's thread on `execution`,
-- and canonical fragment kinds in the delta ledger. Additive; 001 is not edited (MOD-3).

-- A channel thread has no owner: `user_id IS NULL` <=> `channel <> 'panel'`. Kept by the
-- writer, not a CHECK (ER 3823 refuses a CHECK on a column an FK uses).
ALTER TABLE conversation
    MODIFY user_id INT UNSIGNED NULL,
    ADD COLUMN channel VARCHAR(64) NOT NULL DEFAULT 'panel',     -- job.source, or 'panel'
    ADD COLUMN external_ref VARCHAR(512) NULL,                   -- job.reference_id, for display
    -- sha1(source|view|reference): a hash, so a NULL view or a 512-char reference cannot
    -- defeat the unique key. NULL for a panel thread (UNIQUE allows many NULLs).
    ADD COLUMN external_key CHAR(40) NULL,
    ADD COLUMN last_activity_at TIMESTAMP NULL,
    ADD UNIQUE KEY uq_conversation_external (external_key),
    ADD KEY idx_channel_activity (channel, last_activity_at, id);

-- The run's thread, written at claim. Every "which thread is this run" reads it.
ALTER TABLE execution
    ADD COLUMN conversation_id BIGINT UNSIGNED NULL,
    ADD KEY idx_execution_conversation (conversation_id, id);

ALTER TABLE execution_delta MODIFY kind VARCHAR(32) NOT NULL;

UPDATE execution e JOIN message m ON m.job_id = e.job_id
    SET e.conversation_id = m.conversation_id
    WHERE e.conversation_id IS NULL;

UPDATE conversation c SET last_activity_at = COALESCE(
    (SELECT MAX(ev.created_at) FROM conversation_event ev WHERE ev.conversation_id = c.id),
    c.updated_at);
