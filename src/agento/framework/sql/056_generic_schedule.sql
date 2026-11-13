-- AG-67: `schedule` becomes the generic table of every schedule source (docs/architecture/schedules.md).
-- Vendor-free (PLC-2). Legacy rows keep source 'cron' or 'todo' (the ENUM text survives CHANGE)
-- until the owning module converts them. The tick fires only rows of a registered source.
-- Each statement stands alone: the runner skips 1054, 1060, 1061 and 1091 per statement,
-- so a part that an older deployment already has is skipped as a whole.

-- The shape a module migration added before, for a deployment where it never ran (1060 elsewhere).
ALTER TABLE schedule ADD COLUMN agent_view_id INT UNSIGNED NULL AFTER issue_key,
  ADD KEY idx_schedule_agent_view (agent_view_id),
  ADD CONSTRAINT fk_schedule_agent_view FOREIGN KEY (agent_view_id) REFERENCES agent_view(id) ON DELETE SET NULL;

ALTER TABLE schedule CHANGE agent_type source VARCHAR(32) NOT NULL DEFAULT 'scheduler';

ALTER TABLE schedule CHANGE issue_key source_ref VARCHAR(255) NULL;

ALTER TABLE schedule CHANGE summary title VARCHAR(500) NOT NULL DEFAULT '';

ALTER TABLE schedule ADD UNIQUE KEY uq_schedule_agent_view_issue (agent_view_id, source_ref);

-- The old global unique key. 001 named it uq_schedules_issue and 013 renamed only the table,
-- so both spellings are dropped (1091 for the one that is not there).
ALTER TABLE schedule DROP INDEX uq_schedule_issue;

ALTER TABLE schedule DROP INDEX uq_schedules_issue;

-- anchor_local: the last scheduled occurrence, naive local time, before DST normalisation.
ALTER TABLE schedule MODIFY cron_expr VARCHAR(100) NULL,
  ADD COLUMN owner_user_id INT UNSIGNED NULL,
  ADD COLUMN owner_agent_view_id INT UNSIGNED NULL,
  ADD COLUMN prompt MEDIUMTEXT NULL,
  ADD COLUMN rrule VARCHAR(1024) NULL,
  ADD COLUMN starts_at DATETIME NULL,
  ADD COLUMN timezone VARCHAR(64) NULL,
  ADD COLUMN harness VARCHAR(64) NULL,
  ADD COLUMN model VARCHAR(128) NULL,
  ADD COLUMN next_fire_at DATETIME NULL,
  ADD COLUMN last_fire_at DATETIME NULL,
  ADD COLUMN anchor_local DATETIME NULL,
  ADD COLUMN last_error VARCHAR(255) NULL,
  ADD COLUMN deleted_at DATETIME NULL,
  ADD COLUMN created_job_id BIGINT UNSIGNED NULL,
  ADD KEY idx_schedule_due (enabled, next_fire_at),
  ADD KEY idx_schedule_deleted (deleted_at),
  ADD KEY idx_schedule_owner_user (owner_user_id),
  ADD KEY idx_schedule_owner_view (owner_agent_view_id),
  ADD CONSTRAINT fk_schedule_owner_user FOREIGN KEY (owner_user_id) REFERENCES `user`(id) ON DELETE CASCADE,
  ADD CONSTRAINT fk_schedule_owner_view FOREIGN KEY (owner_agent_view_id) REFERENCES agent_view(id) ON DELETE CASCADE;
