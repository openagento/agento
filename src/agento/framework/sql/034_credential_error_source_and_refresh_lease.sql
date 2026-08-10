-- Quarantine provenance + refresh lease for the credential pool.
--
-- error_source distinguishes an automatic (framework) quarantine from an operator
-- decision, so the framework may clear only its own; NULL = unknown provenance,
-- which is treated as operator state and never auto-cleared (pre-migration rows
-- stay exactly as they are).
--
-- lease_owner/leased_until give one job at a time exclusive use of a rotating
-- credential that is close to expiry, so ten concurrent workers cannot each
-- replay the same single-use refresh token. leased_until is a LIVENESS deadline
-- renewed by the owning consumer, not a guess at a job's duration.
--
-- Runs after 030 renamed oauth_token -> credential, so it targets `credential`.
--
-- Separate statements so each converges independently on a drifted DB (the
-- migrator splits on ';' and skips per-statement error 1060 duplicate-column).
ALTER TABLE credential ADD COLUMN error_source ENUM('auto','operator') NULL DEFAULT NULL AFTER error_msg;
ALTER TABLE credential ADD COLUMN lease_owner VARCHAR(64) NULL DEFAULT NULL AFTER throttled_until;
ALTER TABLE credential ADD COLUMN leased_until DATETIME NULL DEFAULT NULL AFTER lease_owner;
