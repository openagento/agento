-- Dry-run mode for evaluation runs (DECISIONS.md D-DRY-RUN-1).
-- job.dry_run: the run's capability is minted with dry_run = 1, and the jobs it creates inherit it.
-- toolbox_capability.dry_run: the toolbox answers a `sensitive` tool with a stub, without the handler.
-- tool_invocation.dry_run: 1 when the stub answered that call.
ALTER TABLE job
  ADD COLUMN dry_run TINYINT(1) NOT NULL DEFAULT 0 AFTER depth;

ALTER TABLE toolbox_capability
  ADD COLUMN dry_run TINYINT(1) NOT NULL DEFAULT 0 AFTER consumed_at;

ALTER TABLE tool_invocation
  ADD COLUMN dry_run TINYINT(1) NOT NULL DEFAULT 0 AFTER outcome;
