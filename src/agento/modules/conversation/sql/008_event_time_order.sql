-- The timeline is in time order, not id order: an imported run (`conversation:backfill`) gets
-- high ids and old times. Microseconds keep the order of one run's events; a tie is broken by
-- id. Old rows get `.000000`, and inside one second their id is the order they were written.
-- This ALTER rebuilds the table.
ALTER TABLE conversation_event
    MODIFY created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    ADD KEY idx_conversation_time (conversation_id, created_at, id);

-- The prune removes rows in `(created_at, id)` order, and this pair is the last row it
-- removed. `last_pruned_event_id` stays the highest id removed: a cursor whose row is gone
-- is expired only at or below it.
ALTER TABLE conversation_prune_watermark
    ADD COLUMN last_pruned_at TIMESTAMP(6) NULL,
    ADD COLUMN last_pruned_at_id BIGINT UNSIGNED NULL;

-- `list_runs` and `list_messages` read a thread in time order. An answer keeps the
-- microseconds of its imported time, so two answers in one second keep their order.
ALTER TABLE execution
    ADD KEY idx_execution_conversation_start (conversation_id, started_at, id);
ALTER TABLE message
    MODIFY created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    ADD KEY idx_message_time (conversation_id, created_at, id);
