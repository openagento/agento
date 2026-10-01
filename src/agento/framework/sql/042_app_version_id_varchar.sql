-- A versioned-artifacts version id is a string (v-YYYYMMDD-HHMMSS-xxxx), not a number.
-- 036/037 declared app_version_id BIGINT. No row sets it yet: only miniapp capabilities do.
ALTER TABLE toolbox_capability MODIFY app_version_id VARCHAR(64) NULL;
ALTER TABLE tool_invocation MODIFY app_version_id VARCHAR(64) NULL;
