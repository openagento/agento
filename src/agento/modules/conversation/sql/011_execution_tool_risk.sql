-- The run's lethal-trifecta assessment (framework/tool_risk.py): written at the mint, then
-- once after the workspace with the direct MCP servers. Admin-only on read. NULL = a run
-- from before the assessment.
ALTER TABLE execution ADD COLUMN tool_risk JSON NULL AFTER credential_id;
