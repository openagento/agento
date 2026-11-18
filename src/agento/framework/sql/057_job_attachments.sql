-- Files a job's run must find under its `./shared/attachments/`.
--
-- A self-contained, vendor-free descriptor:
--   {"key": "<one path component>",
--    "files": [{"from": "<path under the workspace>", "as": "<one path component>"}]}
-- The framework never asks a module what it means; run preparation copies `from` to
-- `<thread shared>/attachments/<key>/<as>` (framework/run_preparation.py).
ALTER TABLE job ADD COLUMN attachments TEXT NULL AFTER context;
