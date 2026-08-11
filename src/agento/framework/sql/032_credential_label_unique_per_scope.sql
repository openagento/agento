-- Label uniqueness is PER SCOPE, not global.
--
-- The constraint was inherited from the pre-0.15 world where one label belonged to one
-- agent type by convention. After the harness/provider split a label is only meaningful
-- inside its credential scope, and `docs/cli/credentials.md` shows `my-token` registered
-- for both `claude` and `codex`.
--
-- With a GLOBAL unique key that second registration did not insert: `register_credential`'s
-- ON DUPLICATE KEY UPDATE matched on label alone and overwrote the first scope's encrypted
-- credentials while LEAVING `scope` untouched (scope is not in the UPDATE list). Net effect:
-- one credential silently destroyed, and the surviving row served the wrong harness's
-- credentials under the other scope's name.
--
-- Widening global -> (scope, label) is strictly weaker, so no existing row can violate it.
ALTER TABLE credential DROP INDEX uq_oauth_token_label;
ALTER TABLE credential ADD UNIQUE KEY uq_credential_scope_label (scope, label);
