-- The runner that started the run, as `<socket name>:<boot id>` (framework/runner/client.py).
-- Liveness, pause and stale recovery ask only that runner, in that boot (the owner rule).
ALTER TABLE job ADD COLUMN runner_ref VARCHAR(128) NULL AFTER pid;
