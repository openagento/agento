-- 046: one row per unbroken run of deferrals on one job (PRD E3-E5 §4.4, §10.1).
--
-- A conversation blocked behind its own earlier turn is offered and deferred once per poll
-- tick. Announcing every deferral would put hundreds of identical events in a thread; the
-- stretch collapses them into one, announced when the stretch CLOSES with the final count.
-- `closed_at IS NULL` means "still blocked", which is why the prune never touches such a row.
CREATE TABLE IF NOT EXISTS job_defer_stretch (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    job_id      BIGINT UNSIGNED NOT NULL,   -- no FK: jobs are pruned on their own schedule
    stretch_seq INT UNSIGNED NOT NULL,
    defer_count INT UNSIGNED NOT NULL DEFAULT 0,
    opened_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at   DATETIME NULL,
    UNIQUE KEY uq_job_stretch (job_id, stretch_seq),
    KEY idx_closed_id (closed_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
