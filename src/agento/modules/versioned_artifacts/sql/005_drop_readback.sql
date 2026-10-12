-- D-MINIAPP-2 (O-4): no copy of a share password is kept that can be read back, and `owner`
-- was a free-text label beside the store's own owner marker.
ALTER TABLE versioned_artifact
    DROP COLUMN auth_secret_enc,
    DROP COLUMN owner;
