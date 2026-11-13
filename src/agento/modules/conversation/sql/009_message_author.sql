-- AG-67 F19: a message carries its poster, and the job it starts is published as that
-- poster, never as the conversation's owner (a channel conversation may have one, or none).
-- SET NULL: a deleted poster leaves the message, and its turn is published with no
-- requester. The backfill covers panel conversations only: their owner was the only
-- possible poster. A legacy channel message stays NULL (it carried `user:None` before).
ALTER TABLE message
    ADD COLUMN author_user_id INT UNSIGNED NULL AFTER role,
    ADD CONSTRAINT fk_message_author FOREIGN KEY (author_user_id)
        REFERENCES `user` (id) ON DELETE SET NULL;

UPDATE message m JOIN conversation c ON c.id = m.conversation_id
    SET m.author_user_id = c.user_id
    WHERE c.channel = 'panel' AND m.role = 'user' AND m.author_user_id IS NULL;
