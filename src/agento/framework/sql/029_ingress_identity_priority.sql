-- 029: add ingress_identity.priority — a selection weight for regex-matched identity types
-- (e.g. outlook_sender). When several bindings of one type match the same inbound identity,
-- the highest priority wins; a tie between DIFFERENT agent_views is ambiguous (no job). Exact
-- (non-regex) types are unaffected (a single row matches). Default 0 preserves existing rows.
-- Two SEPARATE statements (the migrator splits on ';' and skips ignorable per-statement errors
-- 1060 duplicate-column / 1061 duplicate-key): a combined ADD COLUMN + ADD KEY is ONE statement,
-- so on a drifted DB where the column already exists the whole thing (including the index) is
-- skipped wholesale. Split so the column and index each converge independently.
ALTER TABLE ingress_identity
    ADD COLUMN priority INT NOT NULL DEFAULT 0 AFTER agent_view_id;

CREATE INDEX idx_ingress_type_active_priority
    ON ingress_identity (identity_type, is_active, priority);
