-- AG-87: the files of a schedule. The same descriptor as `job.attachments`
-- ({"key", "files": [{"from", "as"}]}); every fire publishes it unchanged.
ALTER TABLE schedule ADD COLUMN attachments TEXT NULL AFTER prompt;
