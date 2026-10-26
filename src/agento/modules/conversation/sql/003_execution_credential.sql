-- E9 run details: the credential an attempt ran on. An id, not the label: a renamed or deleted
-- credential is read through a LEFT JOIN, and no credential value is ever copied here (SEC-1).
-- `harness`, `provider` and `model` exist since 001; the mint fills all four.
ALTER TABLE execution ADD COLUMN credential_id BIGINT UNSIGNED NULL AFTER model;
