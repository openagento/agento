-- A conversation is the timeline of one thread (framework/threads.py): one row per thread,
-- created with the panel conversation or by the first run of a channel thread
-- (`service.link_execution`). The data patch `AssignConversationThreads` fills the rows
-- from before this migration. `external_key` stays, nullable and no longer written, until
-- the next minor release removes it (MOD-3: 002 is not edited).
ALTER TABLE conversation
    ADD COLUMN thread_id BIGINT UNSIGNED NULL AFTER agent_view_id,
    ADD UNIQUE KEY uq_conversation_thread (thread_id),
    ADD CONSTRAINT fk_conversation_thread FOREIGN KEY (thread_id)
        REFERENCES thread (id) ON DELETE SET NULL;
