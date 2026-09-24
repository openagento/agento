-- Auth context v1 (E1). Every column is NULLable on purpose: the verifier is the
-- fail-closed layer, and a NOT NULL default would BE the default the contract forbids.
-- One ALTER per column so a partially applied run converges (the migrator skips 1060/1061).
ALTER TABLE toolbox_capability ADD COLUMN actor VARCHAR(16) NULL;
ALTER TABLE toolbox_capability ADD COLUMN subject_id VARCHAR(128) NULL;
ALTER TABLE toolbox_capability ADD COLUMN on_behalf_of VARCHAR(128) NULL;
ALTER TABLE toolbox_capability ADD COLUMN workspace_id INT UNSIGNED NULL;
ALTER TABLE toolbox_capability ADD COLUMN execution_id VARCHAR(64) NULL;
ALTER TABLE toolbox_capability ADD COLUMN app_artifact_code VARCHAR(64) NULL;
ALTER TABLE toolbox_capability ADD COLUMN app_version_id BIGINT UNSIGNED NULL;
ALTER TABLE toolbox_capability ADD COLUMN app_launch_id VARCHAR(64) NULL;
ALTER TABLE toolbox_capability ADD COLUMN tool_ceiling JSON NULL;
ALTER TABLE toolbox_capability ADD COLUMN allowed_transports JSON NULL;
ALTER TABLE toolbox_capability ADD COLUMN source_kind VARCHAR(32) NULL;
ALTER TABLE toolbox_capability ADD COLUMN source_id VARCHAR(64) NULL;
ALTER TABLE toolbox_capability ADD COLUMN consumed_at DATETIME NULL;
ALTER TABLE toolbox_capability ADD KEY idx_toolbox_capability_source (source_kind, source_id);

-- Backfill: explicit per kind, never a blanket default. The workspace comes from the
-- agent_view the row names (a viewless internal_rest row has none).
UPDATE toolbox_capability c JOIN agent_view av ON av.id = c.agent_view_id
   SET c.workspace_id = av.workspace_id
 WHERE c.workspace_id IS NULL AND c.allowed_transports IS NULL;

-- Legacy MCP tokens keep both transports, on a shortened clock: 4 h, the SSE token TTL.
UPDATE toolbox_capability
   SET actor = 'agent',
       subject_id = CAST(agent_view_id AS CHAR),
       allowed_transports = JSON_ARRAY('sse', 'http'),
       expires_at = LEAST(expires_at, NOW() + INTERVAL 4 HOUR)
 WHERE kind IN ('mcp_job', 'mcp_interactive') AND allowed_transports IS NULL;

-- A pre-E1 internal_rest row stores no component identity, so it gets the named legacy
-- subject rather than an invented one.
UPDATE toolbox_capability
   SET actor = 'service',
       subject_id = 'service:legacy-internal-rest',
       allowed_transports = JSON_ARRAY('http'),
       expires_at = LEAST(expires_at, NOW() + INTERVAL 4 HOUR)
 WHERE kind = 'internal_rest' AND allowed_transports IS NULL
