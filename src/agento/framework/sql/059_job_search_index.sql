-- The panel's job search reads only these columns (framework/admin/data.py `_JOB_SEARCH`). A contains
-- match can use no index range, so it scans: this index (~30 MB at 500k jobs) instead of the job rows,
-- which carry the prompt. `id` is listed although every InnoDB index holds the primary key: MySQL 8.0
-- reads an index as covering for an expression over `id` only when `id` is a named part of it.
ALTER TABLE job ADD KEY idx_job_search (type, status, agent_view_id, reference_id, id);
