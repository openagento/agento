-- Drop the PRE-013 (plural) index names that migrations 030 and 032 missed.
--
-- Migration 005 created `uq_oauth_tokens_label` and `idx_oauth_tokens_agent_enabled` on the
-- then-plural `oauth_tokens` table. Migration 013 renamed only the TABLE — MySQL keeps index
-- names across `RENAME TABLE` — so on every UPGRADED database those indexes are still
-- spelled with the plural. Migrations 030 and 032 drop the singular spellings, and because
-- `migrate.py` swallows error 1091 ("can't DROP; check that it exists") both DROPs became
-- silent no-ops there while the migrations were still recorded as applied.
--
-- Net effect before this migration: a fresh install got `UNIQUE(scope, label)`, but an
-- upgraded install kept the GLOBAL `UNIQUE(label)` — so the cross-scope credential
-- overwrite (a second scope reusing a label overwriting the first scope's credentials via
-- ON DUPLICATE KEY UPDATE) was still live exactly where real data exists.
--
-- Every statement here is idempotent under the swallowed 1091/1061 codes, so this is safe
-- on fresh installs (where the plural names never existed) and on databases that already
-- applied 030/032.
ALTER TABLE credential DROP INDEX uq_oauth_tokens_label;
ALTER TABLE credential DROP INDEX uq_oauth_token_label;
ALTER TABLE credential DROP INDEX idx_oauth_tokens_agent_enabled;
ALTER TABLE credential DROP INDEX idx_oauth_token_agent_enabled;

-- Re-assert the scoped key: 1061 (duplicate key name) is swallowed, so this is a no-op
-- where 032 already succeeded and the real fix where its DROP silently failed.
ALTER TABLE credential ADD UNIQUE KEY uq_credential_scope_label (scope, label);
