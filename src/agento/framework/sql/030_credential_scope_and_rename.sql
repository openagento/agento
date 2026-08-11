-- 030: oauth_token -> credential, agent_type -> scope, usage_log.token_id -> credential_id.
--
-- The harness/provider/credential_scope split means a credential row is no longer keyed by
-- "agent type" (a closed claude|codex enum) but by an open credential SCOPE that a harness
-- declares in its di.json. Same rename pattern as 013_singular_table_names.sql.
--
-- agent_type stays for one cycle as a DUAL-WRITE column, not "legacy read-only": it is
-- NOT NULL (init/000_init.sql), so an INSERT that only filled `scope` would fail. It is
-- widened to VARCHAR(64) first so a longer scope than the old 20-char agent types fits.
-- Dropping the column and the dual-write is a ROADMAP item for the next release.
RENAME TABLE oauth_token TO credential;

ALTER TABLE credential MODIFY agent_type VARCHAR(64) NOT NULL;
ALTER TABLE credential ADD COLUMN scope VARCHAR(64) NULL AFTER agent_type;
UPDATE credential SET scope = agent_type WHERE scope IS NULL;
ALTER TABLE credential MODIFY scope VARCHAR(64) NOT NULL;

-- Pool indexes must follow the new key: LRU selection filters on scope now, so leaving the
-- indexes on agent_type would silently drop the covering index for every credential claim.
CREATE INDEX idx_credential_scope_enabled ON credential (scope, enabled);
CREATE INDEX idx_credential_pool_select ON credential (scope, enabled, status, priority, used_at);
DROP INDEX idx_oauth_token_agent_enabled ON credential;
DROP INDEX idx_oauth_token_pool_select ON credential;

-- usage_log.credential_id becomes NULLABLE: a provider with credential_required=false
-- (e.g. a local model) produces real usage that must still be recorded. harness/provider
-- carry the attribution for those rows, since there is no credential to join to.
ALTER TABLE usage_log DROP FOREIGN KEY fk_usage_token;
ALTER TABLE usage_log DROP INDEX idx_usage_token_time;
ALTER TABLE usage_log CHANGE token_id credential_id BIGINT UNSIGNED NULL;
ALTER TABLE usage_log ADD COLUMN harness VARCHAR(64) NULL AFTER credential_id;
ALTER TABLE usage_log ADD COLUMN provider VARCHAR(64) NULL AFTER harness;
CREATE INDEX idx_usage_credential_time ON usage_log (credential_id, created_at);
ALTER TABLE usage_log
    ADD CONSTRAINT fk_usage_credential FOREIGN KEY (credential_id)
    REFERENCES credential(id) ON DELETE CASCADE;
