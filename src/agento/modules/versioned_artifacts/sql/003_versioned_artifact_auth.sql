ALTER TABLE versioned_artifact
    ADD COLUMN auth_enabled TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN auth_user VARCHAR(255) NULL,
    ADD COLUMN auth_secret_enc TEXT NULL;
