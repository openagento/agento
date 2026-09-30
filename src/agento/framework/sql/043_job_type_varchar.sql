-- PRD E3-E5 §4.2: job.type becomes an open vocabulary.
-- A module declares its own job type; the framework's four built-ins keep their exact
-- values, so every existing row stays valid and no data moves.
ALTER TABLE job MODIFY type VARCHAR(32) NOT NULL;
