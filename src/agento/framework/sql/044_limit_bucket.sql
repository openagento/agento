-- PRD E3-E5 §7.5 — the request limiter's counters. DB-backed, because an in-process
-- counter limits one replica each and the panel may run several.
-- No FK: a bucket key is a hash, and a bucket must outlive whatever it was derived from.
CREATE TABLE IF NOT EXISTS limit_bucket (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    bucket_key CHAR(64) NOT NULL,             -- always sha256 hex, never the credential
    bucket_kind VARCHAR(16) NOT NULL,         -- user | session | launch | address | fallback
    window_start DATETIME NOT NULL,
    request_count INT UNSIGNED NOT NULL DEFAULT 0,
    auth_failures INT UNSIGNED NOT NULL DEFAULT 0,
    held_until DATETIME NULL,
    -- max(window_end, held_until) + core/limits/bucket_retention_seconds: the retention is
    -- a floor on the sweep interval, never a cap on a live row's life.
    expires_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_bucket (bucket_kind, bucket_key),
    KEY idx_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
