-- AG-67: the users a schedule is shared with (edit only, D-3), and the audit of every schedule
-- change. `detail` holds ids and enums only, never the prompt text.
CREATE TABLE IF NOT EXISTS schedule_member (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    schedule_id BIGINT UNSIGNED NOT NULL,
    user_id     INT UNSIGNED NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_schedule_member (schedule_id, user_id),
    KEY idx_schedule_member_user (user_id, schedule_id),
    CONSTRAINT fk_schedule_member_schedule FOREIGN KEY (schedule_id) REFERENCES schedule (id) ON DELETE CASCADE,
    CONSTRAINT fk_schedule_member_user FOREIGN KEY (user_id) REFERENCES `user` (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- No FK on schedule_id: the audit outlives the schedule.
CREATE TABLE IF NOT EXISTS schedule_audit (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    schedule_id BIGINT UNSIGNED NOT NULL,
    action      VARCHAR(32)  NOT NULL,  -- create|update|delete|share|unshare|owner_change|run_now
    actor       VARCHAR(255) NOT NULL,  -- service:web:user:<id> | job:<id> | service:cron:scheduler
    detail      JSON NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    KEY idx_schedule_audit (schedule_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
