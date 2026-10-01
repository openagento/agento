-- The share token (PRD E6 §9): set while Basic auth is on, NULL while it is off.
ALTER TABLE versioned_artifact
    ADD COLUMN share_token CHAR(32) NULL,
    ADD UNIQUE KEY uq_versioned_artifact_share_token (share_token);
