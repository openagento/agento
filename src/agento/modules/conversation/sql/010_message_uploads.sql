-- The files a user attached to a message.
--
--   {"key": "m<message_id>", "files": [{"from": "uploads/t1/u2/<id>/a.pdf", "as": "a.pdf"}]}
-- `key` names the message that FIRST attached them: a regeneration copies this column
-- verbatim, so its turn points at the files the thread already holds.
ALTER TABLE message ADD COLUMN uploads JSON NULL AFTER content;
