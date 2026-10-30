-- Test runs (DECISIONS.md D-TEST-RUN-1).
-- job.test_config: what a test run changes (JSON); the run's capability carries a copy, and the
-- jobs the run creates inherit it.
-- toolbox_capability.test_config: the toolbox reads it per call and answers a stubbed tool itself.
-- tool_invocation.stubbed: 1 when the stub answered the call and the handler did not run.
ALTER TABLE job
  ADD COLUMN test_config JSON NULL;

ALTER TABLE toolbox_capability
  ADD COLUMN test_config JSON NULL;

ALTER TABLE tool_invocation
  ADD COLUMN stubbed TINYINT(1) NOT NULL DEFAULT 0;
